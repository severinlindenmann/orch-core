"""P02 (contract 10.2b): the optional ticket ``description`` and the signed ``audience``. Pure units: the builder, the
rule form and the selection (the vectors are replayed by the generic vector tests of both sides)."""

from __future__ import annotations

import pytest

from orch.hubclient import sharefmt as F
from orch.hubclient import shares as SH
from orch.hubclient.errors import HubRefused

from .test_share_format import HUB, ITEM_ID, WS, _sign_det

U1, U2 = "1" * 32, "2" * 32
AUD = {"users": [U1, U2], "groups": ["team-a"]}
CHAIN = {"log": "01HZZZZZZZZZZZZZZZZZZZZZZZ", "seq": 3, "head": "sha256:" + "a" * 64}
TICKET = {"key": "DEMO-1", "type": "feature", "title": "T", "status": "open", "labels": [], "deadline": None}


def build(content, **kw):
    return F.build_signed_item(
        hub_id=HUB, workspace_id=WS, item_id=ITEM_ID, item_kind=kw.pop("item_kind", "ticket"), item_version=1,
        issued_ms=1, chain=CHAIN, content=content, dev=True, sign=_sign_det, **kw,
    )  # fmt: skip


def test_the_signed_item_carries_the_audience_and_the_description():
    signed = build({**TICKET, "description": "one\ntwo"}, audience=AUD)
    assert signed["o"]["audience"] == AUD and signed["o"]["content"]["description"] == "one\ntwo"
    assert "audience" not in build(TICKET)["o"]  # additive: an item without one is unchanged


@pytest.mark.parametrize(
    "aud",
    [
        {"users": [], "groups": []}, {"users": [U2, U1], "groups": []}, {"users": [], "groups": ["Team A"]},
        {"users": ["zz"], "groups": []}, {**AUD, "roles": []}, {"users": [U1, U1], "groups": []},
        {"users": [], "groups": ["hub-admin"]}, {"users": [U1], "groups": ["hub-admin", "team-a"]},
    ],
)  # fmt: skip
def test_a_bad_audience_is_refused_before_signing(aud):
    with pytest.raises(HubRefused):
        build(TICKET, audience=aud)


def test_an_audience_only_belongs_on_a_ticket_item_and_a_description_only_on_a_ticket():
    with pytest.raises(HubRefused):
        build({"type": "question", "title": "x", "deadline": None}, item_kind="needs_you", audience=AUD)
    with pytest.raises(HubRefused):
        F.check_content("question", {"key": "DEMO-1", "type": "feature", "id": "Q1", "title": "t", "text": "x",
                                     "deadline": None, "description": "no"})  # fmt: skip
    with pytest.raises(HubRefused):
        F.check_content("ticket", {**TICKET, "description": "bad\x07"})


def test_rules_keep_audience_and_description_in_one_stored_form():
    r = SH.normalize_rule(
        {"kind": "tag", "tag": "x", "audience": {"users": [U2, U1], "groups": []}, "description": True}
    )
    assert r == {"kind": "tag", "tag": "x", "audience": {"users": [U1, U2], "groups": []}, "description": True}
    assert SH.normalize_rule(
        {"kind": "tag", "tag": "x", "audience": {"users": [], "groups": []}, "description": False}
    ) == {"kind": "tag", "tag": "x"}  # empty audience = owner only = the old rule
    for bad in ({"kind": "needs_you", "audience": AUD}, {"kind": "tag", "tag": "x", "description": "yes"},
                {"kind": "tag", "tag": "x", "audience": {"users": ["no"], "groups": []}}):  # fmt: skip
        with pytest.raises(HubRefused):
            SH.normalize_rule(bad)


def fact(**kw):
    base = dict(
        uid="01HZZZZZZZZZZZZZZZZZZZZZZZ", key="DEMO-1", type="feature", title="T", status="open", labels=("public",),
        due=None, chain=(3, "sha256:" + "a" * 64), description="Some words.\r\nMore.",
    )  # fmt: skip
    return SH.TicketFact(**{**base, **kw})


def only(rules):
    (item,) = SH.select(rules, [fact()])
    return item


def test_description_and_audience_follow_the_rules_that_select_the_ticket():
    owner_only = only([{"kind": "tag", "tag": "public"}])
    assert "description" not in owner_only.payload and owner_only.audience is None
    both = only([{"kind": "tag", "tag": "public", "audience": AUD, "description": True}])
    assert both.payload["description"] == "Some words.\nMore." and both.audience == AUD
    # fail closed: a second rule without the box keeps the text home; the audiences join
    mixed = only([{"kind": "tag", "tag": "public", "audience": AUD, "description": True},
                  {"kind": "type", "type": "feature", "audience": {"users": [], "groups": ["team-b"]}}])  # fmt: skip
    assert "description" not in mixed.payload and mixed.audience == {"users": [U1, U2], "groups": ["team-a", "team-b"]}


def test_a_change_of_audience_or_description_is_a_new_version_and_a_new_plan():
    a = only([{"kind": "tag", "tag": "public"}])
    b = only([{"kind": "tag", "tag": "public", "audience": AUD}])
    c = only([{"kind": "tag", "tag": "public", "description": True}])
    assert len({a.digest, b.digest, c.digest}) == 3
    assert len({SH.plan_of([a]), SH.plan_of([b]), SH.plan_of([c])}) == 3  # WYSIWYS: the owner confirms each


def test_the_builder_signs_the_audience_of_the_item():
    item = only([{"kind": "tag", "tag": "public", "audience": AUD, "description": True}])
    build_fn = SH.make_builder(
        hub_id=HUB, workspace_id=WS, hxk_pub=SO_HXK(), sign=_sign_det, dev=True, now_ms=lambda: 1
    )
    sealed, ihash = build_fn(ITEM_ID, item, 1)
    assert isinstance(sealed, str) and ihash.startswith("sha256:")


def SO_HXK():
    from .vec import pub

    return pub("hxk")


def test_the_confirmed_grant_is_stored_and_an_unreadable_one_is_refused():
    base = {"rules": [{"kind": "needs_you"}], "sent": {}, "withdrawn": []}
    grant = {"users": [U1], "groups": [], "description": False}
    st = SH.ShareState.from_json({**base, "confirmed": {"ticket:x": grant}})
    assert st.to_json()["confirmed"] == {"ticket:x": grant}
    for bad in (["ticket:x"], {"ticket:x": {**grant, "extra": 1}}, {"ticket:x": {**grant, "description": "yes"}},
                {"ticket:x": {"users": ["no"], "groups": [], "description": False}}, "x", 3):  # fmt: skip
        with pytest.raises(HubRefused):
            SH.ShareState.from_json({**base, "confirmed": bad})


def test_narrowed_never_widens():
    item = only([{"kind": "tag", "tag": "public", "audience": AUD, "description": True}])
    cut = SH.narrowed(item, {"users": [U1], "groups": [], "description": False})
    assert cut.audience == {"users": [U1], "groups": []} and "description" not in cut.payload
    assert SH.narrowed(item, SH.grant_of(item)) is item
    none = SH.narrowed(item, SH.EMPTY_GRANT)
    assert none.audience is None and none.digest != item.digest
