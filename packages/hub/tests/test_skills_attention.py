"""#1617 step 1: the "Needs attention" list is computed from hub data, one plain line with one action each."""

from __future__ import annotations

import pytest
from secrets_helpers import DAY, WS_C, SWeb, join

from orch_hub.activity.store import ActivityLog
from orch_hub.contract import vectorgen as G
from orch_hub.view.skills_attention import needs_attention

NOW = 1_000 * DAY


def sec(name, left_days, state=None):
    return {"name": name, "expires_ms": NOW + int(left_days * DAY), "state": state or {}}


WSS = [{"workspace_id": "a" * 32, "name": "Product"}, {"workspace_id": "b" * 32, "name": None, "owner_name": "Ben"}]


def test_expired_and_soon_expiring_come_first_soonest_first_and_far_ones_are_silent():
    items = needs_attention(
        [sec("far", 90), sec("soon", 3), sec("gone", -2), sec("edge", 14), sec("late", 15)], WSS, NOW
    )
    assert [(i["kind"], i["text"]) for i in items] == [
        ("secret_expired", "gone has expired."),
        ("secret_expiring", "soon expires in 3 days."),
        ("secret_expiring", "edge expires in 14 days."),
    ]
    assert all(i["page"] == "secrets" and i["action"] for i in items)
    assert needs_attention([sec("t", 0.5)], WSS, NOW)[0]["text"] == "t expires today."


def test_a_workspace_without_a_value_is_named_once_or_counted():
    a, b = "a" * 32, "b" * 32
    one = needs_attention([sec("gh", 90, {a: {"state": "ready"}, b: {"state": "needs_value"}})], WSS, NOW)
    assert one == [
        {
            "kind": "secret_waiting",
            "text": "Ben's workspace is waiting for the value of gh.",
            "page": "secrets",
            "action": "Enter the value",
        }
    ]
    two = needs_attention([sec("gh", 90, {a: {"state": "needs_value"}, b: {"state": "needs_value"}})], WSS, NOW)
    assert two[0]["text"] == "2 workspaces are waiting for the value of gh."
    assert needs_attention([sec("gh", 90, {a: {"state": "ready"}})], WSS, NOW) == []


def test_a_workspace_name_is_rendered_safe_and_an_expired_secret_has_one_line_only():
    ws = [{"workspace_id": "a" * 32, "name": "Pro" + chr(0x202E) + "duct"}]
    text = needs_attention([sec("gh", 90, {"a" * 32: {"state": "needs_value"}})], ws, NOW)[0]["text"]
    assert chr(0x202E) not in text and "<U+202E>" in text
    both = needs_attention([sec("gh", -1, {"a" * 32: {"state": "needs_value"}})], ws, NOW)
    assert [i["kind"] for i in both] == ["secret_expired"]


@pytest.fixture
def w(tmp_path, monkeypatch):
    web = SWeb(tmp_path, monkeypatch)
    web.api.d.activity = ActivityLog(":memory:")
    yield web
    web.close()


def test_the_route_reports_a_later_bound_workspace_and_an_expiring_secret_and_is_admin_only(w):
    rec = w.recipients("crm-key", "all", ())
    w.save(rec, "all", (), name="crm-key", expires_ms=G.NOW + 5 * DAY)
    rec2 = w.recipients("gh", "all", ())
    w.save(rec2, "all", (), name="gh")
    s, o, _ = w.call("GET", "/admin/needs-attention", token=w.token)
    assert s == 200 and [i["text"] for i in o["items"]] == ["crm-key expires in 5 days."]
    join(w.registry, WS_C)  # bound later: no wrap exists for it
    s, o, _ = w.call("GET", "/admin/needs-attention", token=w.token)
    assert [i["kind"] for i in o["items"]] == ["secret_expiring", "secret_waiting", "secret_waiting"]
    assert o["items"][1]["text"] == "A workspace is waiting for the value of crm-key."
    s, o, _ = w.call("GET", "/admin/needs-attention")
    assert s in (401, 403)
