"""V3 A7: the hub pages' routes relay the host's reads through a closed shape and build Terminal commands; they sign
nothing and send no hub write to the host."""

import shlex

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orch_dashboard import hub
from orch_dashboard.errors import HostError, fail, host_response

WS = "a" * 32
STATUS = {
    "snapshot": {"version": 4, "roles": {"u1": "admin", "u2": "edit", "u3": "edit", "u4": "read"}},
    "joined": True, "hub_id": "ab12cd34ef56ab12", "mode": "selective", "signing": "selective", "member": False,
    "link": "bound", "unbound_pending": False, "owed": [], "rotation": "none", "reseal_needed": False,
    "log_sync": {"running": True, "last_error": None, "stuck": {}},
}  # fmt: skip
RULES = {
    "rules": [{"kind": "needs_you", "count": 3, "describes": "needs-you summaries"},
              {"kind": "type", "type": "Feature", "count": 1, "describes": "every ticket of type 'Feature'"}],
    "shared": 4, "withdrawn": [], "refused_items": [{"item": "ticket X-1", "code": "hub.refused", "stale": True}],
}  # fmt: skip
PREVIEW = {
    "count": 2, "new": 1, "revoked": 0, "more": 0, "plan": "p",
    "items": [{"logical": "ticket:u", "kind": "ticket", "line": "ticket PRD-0012: Export report as PDF"},
              {"logical": "summary:u", "kind": "summary", "line": "needs you: question: Which greeting?"}],
    "rules": ["every ticket of type 'Feature'"],
}  # fmt: skip


class Link:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def supports(self, op, cap="ops/1"):
        return op in self.answers

    def call(self, op, args=None, **kw):
        self.calls.append((op, args) if not kw else (op, args, kw))
        got = self.answers[op]
        if isinstance(got, Exception):
            raise got
        return got


class Gw:
    def __init__(self, link):
        self._link = link

    def enabled(self, ws):
        return self._link

    def link(self, ws):
        return self._link


def client(link):
    app = FastAPI()
    hub.register(app, Gw(link))

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


def test_status_is_a_closed_shape_with_group_counts():
    link = Link({"hub.status": {**STATUS, "secret": "x", "binding": {"k": 1}}})
    got = client(link).get(f"/api/w/{WS}/hub").json()
    assert got["mode"] == "selective" and got["joined"] and got["hub_id"] == "ab12cd34ef56ab12"
    assert got["groups"] == {"read": 1, "edit": 2, "admin": 1}
    assert "secret" not in got and "binding" not in got
    assert got["reachable"] is None and got["not_told"] is False
    assert link.calls == [("hub.status", None)]


@pytest.mark.parametrize("signing", ["bogus", None, 5])
def test_unknown_mode_fails_closed(signing):
    got = client(Link({"hub.status": {**STATUS, "signing": signing}})).get(f"/api/w/{WS}/hub").json()
    assert got["mode"] == "unknown"


def test_not_told_and_pending_key_change_are_passed_on():
    raw = {
        **STATUS,
        "signing": "standalone",
        "joined": False,
        "unbound_pending": True,
        "owed": ["h1"],
        "rotation": "pending",
    }
    got = client(Link({"hub.status": raw})).get(f"/api/w/{WS}/hub").json()
    assert got["not_told"] and got["owed"] == 1 and got["key_change_pending"]


def test_share_view_lists_rules_and_shared_tickets():
    link = Link({"share.rules": RULES, "share.preview": PREVIEW})
    got = client(link).get(f"/api/w/{WS}/hub/share").json()
    assert got["tickets"] == ["PRD-0012"] and got["tickets_complete"]
    assert [r["kind"] for r in got["rules"]] == ["needs_you", "type"] and got["rules"][1]["value"] == "Feature"
    assert got["refused"] == [{"item": "ticket X-1", "code": "hub.refused", "stale": True}]
    assert link.calls[1] == ("share.preview", {})


def test_share_with_no_hub_is_empty():
    link = Link({"share.rules": fail("hub.not_joined"), "share.preview": PREVIEW})
    got = client(link).get(f"/api/w/{WS}/hub/share").json()
    assert got["rules"] == [] and got["tickets"] == []


def test_hidden_characters_in_host_text_are_withheld():
    bad = {**RULES, "rules": [{"kind": "tag", "tag": "te‮am", "count": 1}]}
    got = client(Link({"share.rules": bad, "share.preview": PREVIEW})).get(f"/api/w/{WS}/hub/share").json()
    assert got["rules"] == []
    p = {**PREVIEW, "items": [{"kind": "ticket", "line": "ticket A-1: ‮x"}]}
    got = (
        client(Link({"share.preview": p}))
        .post(f"/api/w/{WS}/hub/share/preview", json={"add": {"kind": "needs_you"}})
        .json()
    )
    assert got["items"] == []


def test_preview_passes_one_rule_to_the_host():
    link = Link({"share.preview": PREVIEW})
    c = client(link)
    res = c.post(f"/api/w/{WS}/hub/share/preview", json={"add": {"kind": "type", "value": "Bug"}})
    assert res.status_code == 200 and res.json()["new"] == 1
    assert link.calls == [("share.preview", {"add": {"kind": "type", "type": "Bug"}})]
    c.post(f"/api/w/{WS}/hub/share/preview", json={"remove": {"kind": "ticket", "value": "PRD-0014"}})
    assert link.calls[1] == ("share.preview", {"remove": {"kind": "ticket", "ticket": "PRD-0014"}})


@pytest.mark.parametrize("body", [
    {"add": {"kind": "type", "value": "x"}, "remove": {"kind": "type", "value": "y"}},
    {"add": {"kind": "type"}}, {"add": {"kind": "needs_you", "value": "x"}},
    {"add": {"kind": "ticket", "value": "not a key"}}, {"add": {"kind": "tag", "value": "-x"}},
    {"add": {"kind": "tag", "value": "a​b"}}, {"add": {"kind": "tag", "value": " pad"}},
    {"add": {"kind": "other"}}, {"nope": 1},
])  # fmt: skip
def test_preview_refuses_malformed_rules(body):
    link = Link({"share.preview": PREVIEW})
    assert client(link).post(f"/api/w/{WS}/hub/share/preview", json=body).status_code in (
        400,
        422,
    )  # 400 in the real app (invalid.input); the bare test app says 422
    assert link.calls == []


def cmd(c, body):
    return c.post(f"/api/w/{WS}/hub/command", json=body)


def test_commands_are_the_terminal_lines_and_nothing_is_sent_to_the_host():
    link = Link({})
    c = client(link)
    got = cmd(c, {"kind": "join", "invite": "INV-7QK2-M9XD", "card": "/tmp/my hub.json", "mode": "selective"})
    assert got.json()["command"] == "orch hub join INV-7QK2-M9XD --hub-card '/tmp/my hub.json' --mode selective"
    assert (
        cmd(c, {"kind": "mode_full", "card": "card.json"}).json()["command"]
        == "orch hub mode full --hub-card card.json"
    )
    assert cmd(c, {"kind": "leave"}).json()["command"] == "orch hub leave"
    assert cmd(c, {"kind": "leave_hub_down"}).json()["command"] == "orch hub leave"
    assert cmd(c, {"kind": "stop_telling"}).json()["command"] == "orch hub unbind"
    assert cmd(c, {"kind": "share_sync"}).json()["command"] == "orch hub share sync"
    assert cmd(c, {"kind": "share_revoke", "ref": "PRD-0014"}).json()["command"] == "orch hub share revoke PRD-0014"
    r = {"kind": "share_add", "rule": {"kind": "tag", "value": "team; rm -rf ~"}}
    line = cmd(c, r).json()["command"]
    assert shlex.split(line) == ["orch", "hub", "share", "rule", "add", "--tag", "team; rm -rf ~"]
    assert (
        cmd(c, {"kind": "share_remove", "rule": {"kind": "needs_you"}}).json()["command"].endswith("remove --needs-you")
    )
    assert link.calls == []


@pytest.mark.parametrize("body", [
    {"kind": "join", "invite": "INV-1234", "card": "c.json"},  # mode missing
    {"kind": "join", "invite": "bad invite", "card": "c.json", "mode": "full"},
    {"kind": "join", "invite": "INV-1234", "card": "-c.json", "mode": "full"},
    {"kind": "join", "invite": "INV-1234", "card": "c\n.json", "mode": "full"},
    {"kind": "join", "invite": "INV-1234", "card": "c.json", "mode": "all"},
    {"kind": "leave", "card": "c.json"}, {"kind": "mode_full"}, {"kind": "share_add"},
    {"kind": "share_revoke", "ref": "a b"}, {"kind": "hub.join"}, {"kind": "leave", "extra": 1},
])  # fmt: skip
def test_commands_refuse_malformed_requests(body):
    assert cmd(client(Link({})), body).status_code in (
        400,
        422,
    )  # 400 in the real app (invalid.input); the bare test app says 422


def test_old_core_says_so():
    class Old(Link):
        def supports(self, op, cap="ops/1"):
            return False

    res = client(Old({})).get(f"/api/w/{WS}/hub")
    assert res.status_code != 200 and res.json()["error"]["code"] == "click.contract"


def test_module_never_names_a_hub_write():
    import re
    from pathlib import Path

    src = Path(hub.__file__).read_text()
    calls = re.findall(r"\.call\(\s*\"([a-z_.]+)\"", src)
    # connect: C11's one write; addon.list: a read (the join-pause check)
    allowed = {"hub.status", "share.rules", "share.preview", "hub.connect", "hub.link", "addon.list"}
    assert set(calls) <= allowed
    assert src.count("write=True") == 1
    assert "hubclient" not in src and "import orch" not in src


def test_a_host_without_share_ops_degrades_instead_of_failing():
    class NoShare(Link):
        def supports(self, op, cap="ops/1"):
            return op == "hub.status"

    c = client(NoShare({"hub.status": STATUS}))
    assert c.get(f"/api/w/{WS}/hub").status_code == 200
    got = c.get(f"/api/w/{WS}/hub/share")
    assert got.status_code == 200 and got.json()["available"] is False and got.json()["rules"] == []
    res = c.post(f"/api/w/{WS}/hub/share/preview", json={"add": {"kind": "needs_you"}})
    assert res.status_code != 200 and res.json()["error"]["code"] == "op.unsupported"


def test_share_is_available_when_the_host_has_it():
    got = client(Link({"share.rules": RULES, "share.preview": PREVIEW})).get(f"/api/w/{WS}/hub/share").json()
    assert got["available"] is True


# -- #1198 C11: the second half of a join by click --------------------------------------------------------------------

HUB32 = "9f3c1ab277de40c8aa11bb22cc33dd44"
CARD = '{"o": {"hub_id": "' + HUB32 + '"}, "sig": "x"}'
IDEM = "a_01M5PVRB46WS3X8H5X2RQK04S9"


class Auth:
    def __init__(self, kind):
        self.kind = kind

    def login_kind(self, token):
        return self.kind

    def stepup_take(self, token, action, digest):
        return False, None  # no passkey step-up was made


class PausedGw:
    """After the signed hub.join the host pauses every add-on, the dashboard too, until the snapshot is pinned."""

    def __init__(self, link):
        self._link = link

    def enabled(self, ws):
        raise fail("addon.disabled", exit=3)

    def link(self, ws):
        return self._link


def test_connect_is_still_refused_when_the_add_on_is_simply_disabled():
    """Only the fresh-join pause is waived, not a dashboard that is off for another reason."""
    link = Link({
        "hub.connect": {"mode": "selective"},
        "addon.list": {"addons": [{"name": "dashboard", "state": "disabled"}]},
    })

    class Off(PausedGw):
        pass

    got = connect(connect_client(link, gw=Off))
    assert got.status_code != 200 and got.json()["error"]["code"] == "addon.disabled"
    assert not any(c[0] == "hub.connect" for c in link.calls)


def connect_client(link, kind="local", gw=Gw):
    app = FastAPI()
    app.state.auth = Auth(kind)
    hub.register(app, gw(link))

    @app.middleware("http")
    async def token(request, call_next):
        request.state.token = "t"
        return await call_next(request)

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


def connect(c, **over):
    return c.post(f"/api/w/{WS}/hub/connect", json={"idem": IDEM, "invite": "7QK2M9XD", "hub_card": CARD, **over})


def test_connect_relays_the_invite_and_card_to_the_host_as_one_write():
    link = Link({"hub.connect": {"hub_id": HUB32, "status": "bound", "mode": "selective", "binding_seq": 1}})
    got = connect(connect_client(link))
    assert got.status_code == 200 and got.json() == {"connected": True, "mode": "selective"}
    assert link.calls == [
        (
            "hub.connect",
            {"hub_card": {"o": {"hub_id": HUB32}, "sig": "x"}, "invite": "7QK2M9XD"},
            {"write": True, "idem": IDEM},
        )
    ]
    assert "7QK2M9XD" not in got.text  # the answer never repeats the invite


def test_connect_works_while_the_dashboard_add_on_is_paused_by_the_join():
    """e2e-4: join by click signs hub.join, the host pauses all add-ons, and only then the page calls hub/connect."""
    link = Link({
        "hub.connect": {"hub_id": HUB32, "status": "bound", "mode": "selective", "binding_seq": 1},
        "addon.list": {"addons": [{"name": "dashboard", "state": "paused", "paused": {"cause": "unverified"}}]},
    })
    got = connect(connect_client(link, gw=PausedGw))
    assert got.status_code == 200 and got.json() == {"connected": True, "mode": "selective"}
    assert [c[0] for c in link.calls] == ["addon.list", "hub.connect"]


def test_connect_strips_the_hub_id_of_a_full_invite_and_refuses_another_hubs():
    link = Link({"hub.connect": {"mode": "selective"}})
    c = connect_client(link)
    assert connect(c, invite=f"{HUB32}:7QK2M9XD").status_code == 200
    assert link.calls[0][1]["invite"] == "7QK2M9XD"
    other = connect(c, invite="0" * 32 + ":7QK2M9XD")
    assert other.status_code == 400 and len(link.calls) == 1  # nothing was sent


def test_connect_from_a_remote_session_needs_a_passkey_step_up():
    link = Link({"hub.connect": {"mode": "selective"}})
    res = connect(connect_client(link, kind="remote"))
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required" and link.calls == []


@pytest.mark.parametrize(
    "over",
    [
        {"invite": "ab"},
        {"invite": " 7QK2M9XD"},
        {"hub_card": "not json"},
        {"hub_card": "[]"},
        {"idem": "x"},
        {"extra": 1},
    ],
)
def test_connect_refuses_malformed_bodies(over):
    link = Link({"hub.connect": {"mode": "selective"}})
    assert connect(connect_client(link), **over).status_code in (400, 422) and link.calls == []


def test_connect_failure_is_the_hosts_own_answer_and_never_says_connected():
    link = Link({"hub.connect": fail("hub.unreachable")})
    res = connect(connect_client(link))
    assert res.status_code >= 400 and "connected" not in res.text.lower().replace("not connected", "")
    assert res.json()["error"]["code"] == "hub.unreachable"
