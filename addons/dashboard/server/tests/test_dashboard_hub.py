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

    def call(self, op, args=None):
        self.calls.append((op, args))
        got = self.answers[op]
        if isinstance(got, Exception):
            raise got
        return got


class Gw:
    def __init__(self, link):
        self.link = link

    def enabled(self, ws):
        return self.link


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
    assert set(calls) <= {"hub.status", "share.rules", "share.preview"}
    assert "hubclient" not in src and "import orch" not in src
