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


# --- audience_get: the people and teams the owner may name (display data only) ------------------------------------------


def test_audience_get_lists_names_ids_and_team_counts_only(shub):
    acc = access()
    acc.set_disabled(DANA, True)
    h = shub(access=acc)
    got = h.join().request("audience_get", {})
    ids = {p["user_id"] for p in got["people"]}
    assert BEN in ids and CARL in ids
    assert OWNER not in ids and DANA not in ids  # not the owner alias, no disabled person
    assert all(set(p) == {"user_id", "name"} for p in got["people"])
    assert got["teams"] == [{"group": "team-a", "members": 1}]  # counts, no member list, no hub-admin group
    acc.close()


def test_audience_get_is_refused_where_sharing_is_not_possible(shub):
    from shares_helpers import refused

    acc = access()
    h = shub(access=acc)
    refused(h.join(mode="full"), "audience_get", {}, "wrong_mode")
    acc.close()
    h2 = shub()  # no access wired: nothing to list
    refused(h2.join(), "audience_get", {}, "unavailable")


# --- shared_get: what other workspaces shared with the owner of the calling workspace ------------------------------------

WS_BEN = "b" * 32


def ben_ctx(h, acc):
    """Ben's own workspace calling the hub: a registry view where WS_BEN is bound and owned by Ben, WS_A is the real one."""
    from types import SimpleNamespace

    from orch_hub.link import ops

    real = h.svc._reg
    reg = SimpleNamespace(
        lock=real.lock,
        pins=lambda: [G.WS_A, WS_BEN],
        pin=lambda ws: {"wsk_pub": "x"},
        binding_state=lambda ws: {"status": "bound"},
        mode=lambda ws: "selective" if ws == G.WS_A else "full",
        owner_alias=lambda ws: BEN if ws == WS_BEN else real.owner_alias(ws),
    )
    return ops.OpContext(
        hub_id="h", workspace_id=WS_BEN, link_wsk_pub=b"", body={}, registry=reg, invites=None, snapshots=None,
        publish=None, downstream=None, now_ms=0, dev_mode=True, access=acc, shares=h.svc,
    )  # fmt: skip


def test_shared_get_lists_only_tickets_the_signed_audience_names_with_who_shared_them(shub):
    from orch_hub.link import ops

    acc = access()
    h = shub(access=acc)
    c = h.join()
    content = {**V4.TICKET_CONTENT, "description": "Export invoices as CSV."}
    c.request("share_put", ticket_body(IT1, content=content, audience={"users": [BEN], "groups": []}))
    c.request("share_put", ticket_body(IT2))  # no audience: Anna only
    got = ops.shared_get(ben_ctx(h, acc))["items"]
    assert [(i["item_id"], i["shared_by"], i["description"]) for i in got] == [(IT1, "Anna", "Export invoices as CSV.")]
    assert set(got[0]) == {"workspace_id", "item_id", "shared_by", "key", "type", "title", "status", "labels",
                           "deadline", "description"}  # fmt: skip
    acc.set_disabled(BEN, True)
    assert ops.shared_get(ben_ctx(h, acc))["items"] == []  # the read rule is the one of list_shared
    acc.close()


def test_the_hub_admin_group_is_never_a_signed_audience():
    import pytest

    from orch_hub.contract.hub import Refused
    from orch_hub.contract.phase4 import check_audience

    check_audience({"users": [], "groups": ["team-a"]}, "ticket")
    for groups in (["hub-admin"], ["hub-admin", "team-a"]):
        with pytest.raises(Refused):
            check_audience({"users": [], "groups": groups}, "ticket")
