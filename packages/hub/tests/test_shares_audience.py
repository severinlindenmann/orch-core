# ruff: noqa: F811
"""P02: who reads a shared TICKET through its owner-signed audience, and who never does (spec §8a, contract 10.2b)."""

from __future__ import annotations

from shares_helpers import ADMIN, IT1, IT2, OTHER, OWNER, shub  # noqa: F401

from orch_hub.access import Access
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as V4

BEN = G.fake16("ben_user")
CARL = G.fake16("carl_user")  # in the team
DANA = G.fake16("dana_user")  # in no team and not named


def ticket_body(item_id=IT1, version=1, **kw):
    return V4.share_body(
        V4.share_item(version, item_id=item_id, item_kind="ticket", content=kw.pop("content", V4.TICKET_CONTENT), **kw)
    )


def access():
    acc = Access()
    for u, n in ((BEN, "Ben"), (CARL, "Carl"), (DANA, "Dana"), (OWNER, "Anna"), (ADMIN, "Admin"), (OTHER, "Other")):
        acc.add_user(u, n)
    acc.add_group("team-a")
    acc.add_member("team-a", CARL)
    acc.add_member("hub-admin", ADMIN)
    return acc


def test_a_named_person_and_a_team_member_read_the_ticket_and_nobody_else(shub):
    acc = access()
    h = shub(access=acc)
    aud = {"users": [BEN], "groups": ["team-a"]}
    h.join().request("share_put", ticket_body(audience=aud))
    assert [i["item_id"] for i in h.svc.list_shared(OWNER, G.WS_A)] == [IT1]
    assert [i["item_id"] for i in h.svc.list_shared(BEN, G.WS_A)] == [IT1]
    assert [i["item_id"] for i in h.svc.list_shared(CARL, G.WS_A)] == [IT1]
    for nobody in (DANA, ADMIN, OTHER, "", None, "not-hex"):
        assert h.svc.list_shared(nobody, G.WS_A) == []  # type: ignore[arg-type]
    acc.close()


def test_losing_the_team_or_being_disabled_ends_the_access_at_once(shub):
    acc = access()
    h = shub(access=acc)
    h.join().request("share_put", ticket_body(audience={"users": [BEN], "groups": ["team-a"]}))
    acc.remove_member("team-a", CARL)
    assert h.svc.list_shared(CARL, G.WS_A) == []
    acc.set_disabled(BEN, True)
    assert h.svc.list_shared(BEN, G.WS_A) == []
    acc.set_disabled(BEN, False)
    assert len(h.svc.list_shared(BEN, G.WS_A)) == 1
    acc.close()


def test_without_an_audience_or_without_access_only_the_owner_reads(shub):
    acc = access()
    h = shub(access=acc)
    c = h.join()
    c.request("share_put", ticket_body())  # no audience
    assert h.svc.list_shared(BEN, G.WS_A) == [] and len(h.svc.list_shared(OWNER, G.WS_A)) == 1
    acc.close()
    h2 = shub()  # a service with no Access: an audience reads nothing (fail closed)
    h2.join().request("share_put", ticket_body(audience={"users": [BEN], "groups": []}))
    assert h2.svc.list_shared(BEN, G.WS_A) == [] and len(h2.svc.list_shared(OWNER, G.WS_A)) == 1


def test_an_unknown_group_reads_nothing_and_a_role_grant_does_not_help(shub):
    acc = access()
    acc.grant(G.WS_A, "team-a", "admin")  # a hub role in a selective workspace still grants nothing
    h = shub(access=acc)
    h.join().request("share_put", ticket_body(audience={"users": [], "groups": ["ghost"]}))
    assert h.svc.list_shared(CARL, G.WS_A) == [] and h.svc.list_shared(DANA, G.WS_A) == []
    acc.close()


def test_an_audience_reader_can_read_but_never_sign(shub):
    acc = access()
    h = shub(access=acc)
    h.join().request("share_put", ticket_body(audience={"users": [BEN], "groups": []}))
    try:
        h.svc.shared_item(BEN, G.WS_A, IT1)
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", "") == "not_granted"
    else:
        raise AssertionError("an audience reader must not get the signing view")
    assert h.svc.shared_item(OWNER, G.WS_A, IT1)["item_id"] == IT1
    acc.close()


def test_the_description_travels_with_the_item(shub):
    acc = access()
    h = shub(access=acc)
    content = {**V4.TICKET_CONTENT, "description": "Line one\nLine two"}
    h.join().request("share_put", ticket_body(content=content, audience={"users": [BEN], "groups": []}))
    (row,) = h.svc.list_shared(BEN, G.WS_A)
    assert row["item"]["content"]["description"] == "Line one\nLine two"
    acc.close()
