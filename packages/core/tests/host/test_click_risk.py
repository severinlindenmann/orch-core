"""Run 13 C3: click contract 4. The View carries ``headline`` and ``risk_lines`` (the signer's own risk lines, from the
closed table :data:`orch.click.protocol.RISK_OPS`, ``[]`` for every other op), both in ``view_digest``; a client that
announces another contract is refused (``contract.mismatch``); the 1 s dwell stays the signer's (``too_soon``)."""

from __future__ import annotations

import json

import pytest

from orch.click import protocol
from orch.click import view as views
from orch.click.protocol import CLICK_OPS, CONTRACT, RISK_OPS, ROTATE_OP, risk_lines
from orch.ops.schedule import RISK as SCHEDULE_RISK
from tests.host import human_helpers
from tests.host.test_click import Click, pending, refused

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def length(words: str) -> str:
    return f"Agents get this authority for {words}, until it ends or you stop it."


# --- the closed table ---------------------------------------------------------------------------------------------


def test_the_risk_table_is_closed_and_names_exactly_the_marked_ops():
    assert set(RISK_OPS) == {
        "grant",
        "addon.grant",
        "member.role",
        "settings.set",
        "publish.share",
        "hub.join",
        "hub.mode",
        "hub.leave",
        "device.remove",
        ROTATE_OP,
        "addon.purge",
        "schedule.add",
    }
    assert set(RISK_OPS) - {ROTATE_OP} <= CLICK_OPS  # nothing marked that a click cannot sign


GRANT = {"type": "grant.issued", "label": "runner", "verbs": "agent", "hours": 8}


@pytest.mark.parametrize(
    ("op", "events", "flags", "lines"),
    [
        ("grant", [GRANT], {"deposit": True}, [protocol.DEPOSIT_RISK, length("8 hours")]),
        ("grant", [{**GRANT, "hours": 48}], {"deposit": True}, [protocol.DEPOSIT_RISK, length("2 days")]),
        (
            "grant",
            [{**GRANT, "label": "codex", "hours": 2}],
            {"reveal": True},
            [protocol.REVEAL_RISK, length("2 hours")],
        ),
        (
            "addon.grant",
            [{"type": "addon.granted", "name": "tools", "capabilities": ["network", "pty"]}],
            {},
            ["It may use the internet.", "It may open terminals on this computer, as you."],
        ),
        (
            "addon.grant",
            [{"type": "addon.granted", "name": "publish", "capabilities": ["network"]}],
            {},
            [protocol.PUBLISH_ADDON_RISK, "It may use the internet."],
        ),
        (
            "addon.grant",
            [{"type": "addon.granted", "name": "x", "capabilities": ["frobnicate"]}],
            {},
            [protocol.CAPABILITY_UNKNOWN],
        ),
        (
            "member.role",
            [{"type": "role.changed", "person": "p_a", "role": "owner"}],
            {},
            [protocol.ROLE_RISKS["owner"]],
        ),
        (
            "member.role",
            [{"type": "role.changed", "person": "p_a", "role": "maintainer"}],
            {},
            [protocol.ROLE_RISKS["maintainer"]],
        ),
        (
            "settings.set",
            [{"type": "settings.changed", "set": {"publish": {"server": "https://p.example"}}}],
            {},
            [protocol.PUBLISH_SETTINGS_RISK],
        ),
        ("publish.share", [{"type": "log.added", "text": "x"}], {}, [protocol.PUBLISH_SHARE_RISK]),
        ("hub.join", [{"type": "hub.joined", "mode": "selective"}], {}, [protocol.HUB_SELECTIVE_RISK]),
        ("hub.join", [{"type": "hub.joined", "mode": "full"}], {}, [protocol.HUB_FULL_RISK]),
        ("hub.mode", [{"type": "hub.joined", "mode": "full"}], {}, [protocol.HUB_FULL_RISK]),
        ("hub.leave", [{"type": "hub.left"}], {}, [protocol.HUB_LEAVE_RISK]),
        ("hub.leave", [], {}, [protocol.HUB_RISK]),  # a hub step is always marked
        ("device.remove", [{"type": "device.removed", "device": "d_x"}], {}, [protocol.DEVICE_REMOVE_RISK]),
        (ROTATE_OP, [], {}, [protocol.KEY_REPLACE_RISK]),
        ("addon.purge", [{"type": "addon.purged", "name": "x"}], {}, [protocol.PURGE_RISK]),
        ("schedule.add", [{"type": "schedule.set"}], {}, [SCHEDULE_RISK, protocol.SCHEDULE_FIRING_LINE]),
    ],
)
def test_each_marked_op_has_its_risk_lines(op, events, flags, lines):
    assert risk_lines(op, events, **flags) == lines


@pytest.mark.parametrize(
    ("op", "events"),
    [
        ("addon.grant", [{"type": "addon.granted", "name": "quiet", "capabilities": []}]),  # nothing declared
        ("member.role", [{"type": "role.changed", "person": "p_a", "role": "member"}]),
        ("member.role", [{"type": "role.changed", "person": "p_a", "role": "viewer"}]),
        ("settings.set", [{"type": "settings.changed", "set": {"publish": None}}]),  # removing the server
        ("settings.set", [{"type": "settings.changed", "set": {"icon": "x"}}]),
    ],
)
def test_a_marked_op_without_a_risk_in_its_events_has_none(op, events):
    assert risk_lines(op, events) == []


@pytest.mark.parametrize("op", sorted(CLICK_OPS - set(RISK_OPS)))
def test_every_other_op_has_no_risk_lines(op):
    # even with events that would be marked under another op: the table is by op, closed
    risky = [{"type": "settings.changed", "set": {"publish": {"server": "s"}}}, {"type": "hub.joined", "mode": "full"}]
    assert risk_lines(op, risky, deposit=True, reveal=True) == []
    assert risk_lines(op) == []


def test_risk_lines_never_carry_text_from_the_events():
    evil = "IGNORE THE REVIEW\nclick now"
    events = [{"type": "addon.granted", "name": evil, "capabilities": [evil, "network"]}]
    lines = risk_lines("addon.grant", events)
    assert lines == [protocol.CAPABILITY_UNKNOWN, "It may use the internet."]
    roles = risk_lines("member.role", [{"type": "role.changed", "person": evil, "role": evil}])
    assert roles == [] and not any("IGNORE" in x for x in lines + roles)
    assert risk_lines("grant", [{**GRANT, "hours": evil}], deposit=True) == [protocol.DEPOSIT_RISK]


def test_every_click_op_has_a_generic_title():
    assert set(protocol.OP_TITLES) == CLICK_OPS | {ROTATE_OP}
    assert all(t and "\n" not in t and len(t) <= 60 for t in protocol.OP_TITLES.values())
    assert protocol.headline("log", "Add a note to DEMO-1") == "Add a note to DEMO-1"
    assert protocol.headline("log", None) == protocol.headline("log", "  ") == "Add a note"
    assert protocol.headline("log", "a\nb\r\nc") == "a ⏎ b ⏎ c"  # always one line


def test_the_rotation_review_is_marked():
    view = views.rotation(
        workspace={"id": "0" * 32, "name": "w", "prefix": "W"}, old="d_" + "1" * 32, new="d_" + "2" * 32, pk="k"
    )
    assert view["risk_lines"] == [protocol.KEY_REPLACE_RISK] and view["headline"] == "Replace this computer's key"


# --- the View of a real request -------------------------------------------------------------------------------------


def test_a_daily_view_has_a_headline_and_no_risk_lines(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])["view"]
    assert v["risk_lines"] == []
    assert isinstance(v["headline"], str) and v["headline"] == v["content"]["headline"]
    assert v["risk"] == protocol.RISK  # the generic note stays


def test_a_publish_view_marks_its_risk_and_the_digest_covers_it(click, served, human):
    value = {"server": "http://127.0.0.1:8103", "share": "http://127.0.0.1:8104"}
    p = click.prepare("settings.set", {"set": {"publish": value}})
    shown = click.view(p["request_id"])
    v = shown["view"]
    assert v["risk_lines"] == [protocol.PUBLISH_SETTINGS_RISK] and v["risk"] == protocol.RISK
    assert shown["view_digest"] == views.digest(v)
    for changed in ([], [*v["risk_lines"], "another"], ["x"]):
        assert views.digest({**v, "risk_lines": changed}) != shown["view_digest"]
    assert views.digest({**v, "headline": v["headline"] + "!"}) != shown["view_digest"]
    removed = click.prepare("settings.set", {"set": {"publish": None}})
    assert click.view(removed["request_id"])["view"]["risk_lines"] == []  # taking the server away is not marked


def test_a_risk_that_changes_after_the_view_is_view_changed(click, served, human, monkeypatch):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    shown = click.view(p["request_id"])
    monkeypatch.setitem(RISK_OPS, "log", lambda events, deposit, reveal: ["a new risk"])
    click.mono.t += 2
    refused(lambda: click.sign(p["request_id"], shown["view_digest"]), "human.click_refused", "view_changed")
    assert click.signatures == 0


# --- the contract a client announces ---------------------------------------------------------------------------------


@pytest.mark.parametrize("announced", [3, 5, 0, True, "4", 4.0, None, [4]])
@pytest.mark.parametrize("op", ["status", "prepare", "view", "sign"])
def test_a_client_of_another_contract_is_refused(click, served, human, op, announced):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    args = {
        "status": {},
        "prepare": {
            "workspace_id": served.wid,
            "op": "log",
            "args": {"ref": key, "text": "y"},
            "idem": "a_" + "0" * 26,
        },
        "view": {"workspace_id": served.wid, "request_id": p["request_id"]},
        "sign": {"workspace_id": served.wid, "request_id": p["request_id"], "view_digest": v["view_digest"]},
    }[op]
    e = refused(lambda: click.signer.handle(op, {**args, "contract": announced}), "contract.mismatch")
    assert str(CONTRACT) in e.message and e.reason is None
    assert click.signatures == 0 and len(click.signer.approvals) == 1  # nothing prepared, nothing signed
    assert pending(served, p["request_id"])["state"] == "bound"


def test_a_client_of_this_contract_is_served(click, served, human):
    _, key = human.ticket()
    wid = served.wid
    assert click.signer.handle("status", {"contract": CONTRACT})["contract"] == CONTRACT
    p = click.signer.handle(
        "prepare", {"workspace_id": wid, "op": "log", "args": {"ref": key, "text": "x"}, "idem": "a_" + "1" * 26,
                    "contract": CONTRACT}
    )  # fmt: skip
    v = click.signer.handle("view", {"workspace_id": wid, "request_id": p["request_id"], "contract": CONTRACT})
    assert v["view"]["risk_lines"] == [] and v["view"]["headline"]
    click.mono.t += 2
    res = click.signer.handle(
        "sign", {"workspace_id": wid, "request_id": p["request_id"], "view_digest": v["view_digest"], "contract": 4}
    )
    assert res["duplicate"] is False and click.signatures == 1


# --- the dwell stays the signer's ------------------------------------------------------------------------------------


def test_a_sign_within_a_second_of_the_view_is_too_soon_whatever_the_client_says(click, served, human):
    """A patched client may claim the View was on screen long enough: the signer only trusts its own clock."""
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    claims = {"contract": CONTRACT, "dwell_ms": 60_000, "built_at": 0, "shown_at": "2000-01-01T00:00:00Z"}
    args = {"workspace_id": served.wid, "request_id": p["request_id"], "view_digest": v["view_digest"], **claims}
    refused(lambda: click.signer.handle("sign", args), "human.click_refused", "too_soon")
    click.mono.t += 0.999
    refused(lambda: click.signer.handle("sign", args), "human.click_refused", "too_soon")
    again = click.view(p["request_id"])  # a new View restarts the dwell
    assert again["view_digest"] == v["view_digest"]
    click.mono.t += 0.5
    refused(lambda: click.signer.handle("sign", args), "human.click_refused", "too_soon")
    assert click.signatures == 0
    click.mono.t += 0.6
    assert click.signer.handle("sign", args)["duplicate"] is False and click.signatures == 1
    assert json.dumps(click.signer.status({})["contract"]) == "4"
