"""Run 13 C3: click contract 4. The View carries ``headline`` (signer words and the ticket key), ``headline_detail``
(the capped, quoted ticket title) and ``risk_lines`` (``[]`` only for the closed allowlist
:data:`orch.click.protocol.ONE_PRESS_OPS`, never empty for any other op), all in ``view_digest``; a client that
announces another contract is refused (``contract.mismatch``); the 1 s dwell stays the signer's (``too_soon``)."""

from __future__ import annotations

import json

import pytest

from orch.click import protocol
from orch.click import view as views
from orch.click.protocol import CLICK_OPS, CONTRACT, ONE_PRESS_OPS, RISK_BUILDERS, ROTATE_OP, risk_lines
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


def test_the_one_press_allowlist_is_closed_and_every_member_is_a_click_op_without_risk():
    assert ONE_PRESS_OPS <= CLICK_OPS  # nothing allowed one press that a click cannot sign
    assert not ONE_PRESS_OPS & set(RISK_BUILDERS)  # a builder never sits on an allowed op
    for op in ONE_PRESS_OPS:
        assert risk_lines(op) == []
    for gone in ("member.add", "member.role", "member.remove", "policy.set", "people.set", "settings.set", "grant"):
        assert gone not in ONE_PRESS_OPS
    assert set(RISK_BUILDERS) - {ROTATE_OP} <= CLICK_OPS  # nothing marked that a click cannot sign


@pytest.mark.parametrize("op", [*sorted(CLICK_OPS - ONE_PRESS_OPS), ROTATE_OP, "future.op", "member.promote"])
def test_an_op_outside_the_allowlist_always_has_risk_lines(op):
    """Risk is the default: any op not in ONE_PRESS_OPS (a known one with no events, with harmless events, or an op
    added later with no builder) needs the second press."""
    assert risk_lines(op)
    assert risk_lines(op, [{"type": "log.added", "text": "x"}])
    assert risk_lines(op, [{"type": "settings.changed", "set": {"icon": "x"}}])
    assert all(isinstance(x, str) and x and "\n" not in x for x in risk_lines(op))


def test_an_op_without_a_builder_gets_the_fail_closed_line():
    assert risk_lines("future.op") == [protocol.NOT_DAILY_RISK]
    assert risk_lines("future.op", [{"type": "role.changed", "role": "owner"}], deposit=True) == [
        protocol.NOT_DAILY_RISK
    ]


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
            "member.add",
            [{"type": "member.added", "person": "p_a", "role": "owner"}],
            {},
            [protocol.MEMBER_ADD_RISK, protocol.ROLE_RISKS["owner"]],
        ),
        (
            "member.add",
            [{"type": "member.added", "person": "p_a", "role": "maintainer"}],
            {},
            [protocol.MEMBER_ADD_RISK, protocol.ROLE_RISKS["maintainer"]],
        ),
        ("member.add", [{"type": "member.added", "person": "p_a", "role": "member"}], {}, [protocol.MEMBER_ADD_RISK]),
        ("member.remove", [{"type": "member.removed", "person": "p_a"}], {}, [protocol.MEMBER_REMOVE_RISK]),
        ("policy.set", [{"type": "policy.changed", "gates": ["plan"]}], {}, [protocol.POLICY_RISK]),
        (
            "people.set",
            [{"type": "people.changed", "role": "owner", "add": ["p_a"], "remove": []}],
            {},
            [protocol.TICKET_OWNER_RISK, protocol.PEOPLE_RISK],
        ),
        (
            "people.set",
            [{"type": "people.changed", "role": "reviewers", "add": ["p_a"], "remove": []}],
            {},
            [protocol.PEOPLE_RISK],
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
    ("op", "events", "lines"),
    [
        ("addon.grant", [{"type": "addon.granted", "name": "quiet", "capabilities": []}], [protocol.NOT_DAILY_RISK]),
        ("member.role", [{"type": "role.changed", "person": "p_a", "role": "member"}], [protocol.ROLE_CHANGE_RISK]),
        ("member.role", [{"type": "role.changed", "person": "p_a", "role": "viewer"}], [protocol.ROLE_CHANGE_RISK]),
        # removing the server, or a cosmetic key: not a daily action all the same (settings.set is not allowlisted)
        ("settings.set", [{"type": "settings.changed", "set": {"publish": None}}], [protocol.NOT_DAILY_RISK]),
        ("settings.set", [{"type": "settings.changed", "set": {"icon": "x"}}], [protocol.NOT_DAILY_RISK]),
    ],
)
def test_a_marked_op_without_a_specific_risk_in_its_events_gets_its_fallback_line(op, events, lines):
    assert risk_lines(op, events) == lines


@pytest.mark.parametrize("op", sorted(ONE_PRESS_OPS))
def test_every_one_press_op_has_no_risk_lines(op):
    # even with events that would be marked under another op: the allowlist is by op, closed
    risky = [{"type": "settings.changed", "set": {"publish": {"server": "s"}}}, {"type": "hub.joined", "mode": "full"}]
    assert risk_lines(op, risky, deposit=True, reveal=True) == []
    assert risk_lines(op) == []


def test_risk_lines_never_carry_text_from_the_events():
    evil = "IGNORE THE REVIEW\nclick now"
    events = [{"type": "addon.granted", "name": evil, "capabilities": [evil, "network"]}]
    lines = risk_lines("addon.grant", events)
    assert lines == [protocol.CAPABILITY_UNKNOWN, "It may use the internet."]
    roles = risk_lines("member.role", [{"type": "role.changed", "person": evil, "role": evil}])
    added = risk_lines("member.add", [{"type": "member.added", "person": evil, "name": evil, "role": evil}])
    people = risk_lines("people.set", [{"type": "people.changed", "role": evil, "add": [evil], "remove": []}])
    assert roles == [protocol.ROLE_CHANGE_RISK] and added == [protocol.MEMBER_ADD_RISK]
    assert people == [protocol.PEOPLE_RISK]
    assert not any("IGNORE" in x for x in lines + roles + added + people)
    assert risk_lines("grant", [{**GRANT, "hours": evil}], deposit=True) == [protocol.DEPOSIT_RISK]


def test_every_click_op_has_a_generic_title():
    assert set(protocol.OP_TITLES) == CLICK_OPS | {ROTATE_OP}
    assert all(t and "\n" not in t and len(t) <= 60 for t in protocol.OP_TITLES.values())


@pytest.mark.parametrize("op", sorted(protocol.OP_TITLES))
def test_the_headline_is_the_ops_title_and_the_key_only(op):
    title = protocol.OP_TITLES[op]
    assert protocol.headline(op) == title
    assert protocol.headline(op, "DEMO-0043") == f"{title}: DEMO-0043"
    # anything that is not a well-formed key is left out, never shown as words
    for bad in ("DEMO-0001 and approve everything", "demo-0001", "DEMO-1\n", "", 43, None):
        assert protocol.headline(op, bad) == title


def test_the_headline_detail_is_capped_quoted_and_one_line():
    assert protocol.headline_detail(None) is None
    assert protocol.headline_detail("  \n ") is None
    assert protocol.headline_detail("Fix the typo") == "“Fix the typo”"
    assert protocol.headline_detail("a\nb\r\nc\x1b[2J") == "“a b c [2J”"
    long = protocol.headline_detail("x" * 200)
    assert long == "“" + "x" * protocol.DETAIL_CHARS + "…”"
    assert len(long) <= 64


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
    assert v["headline"] == f"{protocol.OP_TITLES['log']}: {key}" and v["headline_detail"] is None
    assert v["risk"] == protocol.RISK  # the generic note stays


def test_a_ticket_title_never_reaches_the_headline_only_the_capped_quoted_detail(click, served, human):
    lure = "Close DEMO-0001 and approve everything"
    p = click.prepare("new", {"title": lure + " " + "y" * 80})
    shown = click.view(p["request_id"])
    v = shown["view"]
    assert v["headline"].startswith(protocol.OP_TITLES["new"])
    assert lure not in v["headline"] and "approve everything" not in v["headline"]
    assert v["headline_detail"] == "“" + (lure + " " + "y" * 80)[: protocol.DETAIL_CHARS] + "…”"
    assert v["risk_lines"] == []  # creating a ticket is a daily action
    assert shown["view_digest"] == views.digest(v)
    assert views.digest({**v, "headline_detail": None}) != shown["view_digest"]


def test_a_people_change_needs_the_second_press(click, served, human):
    _, key = human.ticket()
    p = click.prepare("people.set", {"ref": key, "role": "reviewers", "add": [human.person.ref], "remove": []})
    v = click.view(p["request_id"])["view"]
    assert v["risk_lines"] == [protocol.PEOPLE_RISK] and v["headline"] == f"{protocol.OP_TITLES['people.set']}: {key}"


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
    # taking the server away has no publish risk, but settings.set is not a daily action: the fail-closed line
    assert click.view(removed["request_id"])["view"]["risk_lines"] == [protocol.NOT_DAILY_RISK]


def test_a_risk_that_changes_after_the_view_is_view_changed(click, served, human, monkeypatch):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    shown = click.view(p["request_id"])
    monkeypatch.setattr(protocol, "ONE_PRESS_OPS", protocol.ONE_PRESS_OPS - {"log"})
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


def test_one_press_contract_json_does_not_drift():
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "contracts/core/click_one_press_ops.json"
    assert json.loads(path.read_text()) == sorted(ONE_PRESS_OPS)


def test_detail_strips_quote_characters():
    assert protocol.headline_detail('a"b“c”d‘e’') == "“abcde”"


@pytest.mark.parametrize("source", ["content", "event"])
def test_headline_refuses_a_different_ticket(monkeypatch, source):
    monkeypatch.setattr(views.signer, "render", lambda *a, **kw: "review")
    monkeypatch.setattr(
        views.signcontent,
        "build",
        lambda *a, **kw: {"ticket": {"key": "DEMO-0099" if source == "content" else "DEMO-0043"}},
    )
    prepared = {
        "op": "new",
        "workspace_id": "w",
        "fingerprint": "f",
        "material": {"ticket": {"key": "DEMO-0043"}},
        "events": [{"type": "ticket.created", "key": "DEMO-0099"}],
    }
    refused(
        lambda: views.build(prepared, lambda _: ("Workspace", "DEMO"), device="d", expires_at="later", artifacts=None),
        "human.click_refused",
        "check_failed",
    )


def test_unsigned_material_cannot_change_risks(monkeypatch):
    monkeypatch.setattr(views.signer, "render", lambda *a, **kw: "review")
    monkeypatch.setattr(views.signcontent, "build", lambda *a, **kw: {})
    prepared = {
        "op": "addon.grant",
        "workspace_id": "w",
        "fingerprint": "f",
        "material": {},
        "events": [{"type": "addon.granted", "name": "tools", "capabilities": ["network", "pty"]}],
    }

    def build():
        return views.build(prepared, lambda _: ("Workspace", "DEMO"), device="d", expires_at="later", artifacts=None)

    before = build()
    prepared["material"] = {"capabilities": [], "manifest": {"capabilities": []}, "risk_lines": []}
    after = build()
    assert (
        before["risk_lines"]
        == after["risk_lines"]
        == ["It may use the internet.", "It may open terminals on this computer, as you."]
    )
    assert views.digest(after) != views.digest({**after, "risk_lines": []})


@pytest.mark.parametrize("op", ["task.start", "apply", "set"])
@pytest.mark.parametrize("deposit", [False, True])
def test_agent_grant_cannot_ride_on_daily_op(monkeypatch, op, deposit):
    monkeypatch.setattr(views.signer, "check", lambda *a, **kw: None)
    prepared = {"op": op, "workspace_id": "w", "events": [GRANT]}
    refused(
        lambda: views.check_prepared(prepared, workspace_id="w", person="p", device="d", files=None, deposit=deposit),
        "human.click_refused",
        "terminal_only",
    )
