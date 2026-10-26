"""C3b task 7 (ticket-format 15.4 views): a hub-signed event reads as the person, then ``via hub · <factor>``, in the
read model, ``orch log`` / ``orch show`` and the workspace feed; ``hub.pinned`` reads as "hub key/roles updated"."""

from __future__ import annotations

from types import SimpleNamespace

from orch.host import readmodel
from orch.ops import views

CHAN, HUB = "c" * 32, "d" * 32
USER = "a368bd380588e5dabee62a516031e21f"


def hub_event(**over):
    e = {
        "seq": 4, "type": "log.added", "text": "from my phone", "auth": "hub", "channel_factor": "se-unlocked",
        "hub_id": HUB, "actor": {"kind": "person", "id": "u_" + USER, "via": CHAN}, "at": "2026-10-23T10:00:00Z",
    }  # fmt: skip
    return {**e, **over}


def test_the_actor_carries_via_and_the_member_it_is_when_aliased():
    ws = SimpleNamespace(hub={"alias_book": {HUB: {"4c" * 16: USER}}, "status": "joined"})
    a = readmodel.actor(hub_event(), ws)
    assert a["via"] == CHAN and a["kind"] == "person" and a["auth"] == "hub"
    assert a["person"] == "p_" + "4c" * 16  # derived from the alias book in the log, never stored
    plain = readmodel.actor(hub_event(), SimpleNamespace(hub={}))
    assert "person" not in plain and plain["id"] == "u_" + USER
    device = readmodel.actor({"auth": "passphrase", "actor": {"kind": "person", "id": "p_x", "device": "d_y"}})
    assert "via" not in device and "person" not in device


def test_the_event_row_names_the_factor_and_hub_only_for_hub_events():
    assert readmodel.hub_row(hub_event()) == {"channel_factor": "se-unlocked", "hub_id": HUB}
    assert readmodel.hub_row({"auth": "passphrase", "channel_factor": "x"}) == {}
    row = readmodel.activity_item(
        7, "t", {**hub_event(), "ws_seq": 2}, key="DEMO-1", title="T", summary_text="s", data={}, ws=None
    )
    assert row["channel_factor"] == "se-unlocked" and row["hub_id"] == HUB and row["actor"]["via"] == CHAN


def test_log_lines_say_hub_user_and_the_factor_in_words():
    line = views.event_line(hub_event())
    assert line == f"#4 log.added hub user {USER[:8]} via hub · phone, unlocked: from my phone"
    assert "se-unlocked" not in line
    for factor, words in (("password+totp", "browser, password + code"), ("passkey", "passkey")):
        assert words in views.event_line(hub_event(channel_factor=factor))
    assert views.event_line({**hub_event(), "actor": {"kind": "person", "id": "p_" + "4c" * 16, "device": "d_1"},
                             "auth": "passphrase"}).startswith("#4 log.added person p_4c4c4c")  # fmt: skip


def test_a_hub_pinned_line_reads_as_hub_key_roles_updated_with_kind_and_version():
    e = {
        "seq": 9, "type": "hub.pinned", "actor": {"kind": "host"}, "hub_id": HUB,
        "object": {"o": {"kind": "hub.snapshot", "version": 3, "hub_id": HUB}},
    }  # fmt: skip
    assert views.event_line(e) == "#9 hub.pinned host: hub key/roles updated (snapshot v3)"
    assert readmodel.ws_data(e) == {"hub_id": HUB, "kind": "hub.snapshot", "version": 3}
    card = {**e, "object": {"o": {"kind": "hub.card", "hub_id": HUB}}}
    assert views.event_line(card).endswith("hub key/roles updated (card)")
    assert readmodel.ws_event_visible("hub.pinned", "p_x", False, None)  # every member with a person sees the line
    assert readmodel.summary(e) == "hub key/roles updated (snapshot v3)"


def test_orch_log_names_the_member_a_hub_user_is_aliased_to():
    ws = SimpleNamespace(
        hub={"alias_book": {HUB: {"4c" * 16: USER}}},
        members={"p_" + "4c" * 16: SimpleNamespace(name="Mara")},
    )
    names = views.hub_names(ws)
    assert names == {"u_" + USER: "Mara"}
    assert views.event_line(hub_event(), None, names) == "#4 log.added Mara via hub · phone, unlocked: from my phone"
    assert views.event_line(hub_event(), None, {}).startswith(f"#4 log.added hub user {USER[:8]} via hub")
