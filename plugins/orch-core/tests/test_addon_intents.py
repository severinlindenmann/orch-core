"""Ruling R-A1-INTENT: addon code never receives an Ops. resolve()/act() return an Intent; core checks it against what
the human clicked on and executes it as the human."""
import pytest

from addon_fixtures import loaded
from orch.addons.api import Intent, PendingDecision
from orch.addons.loader import AddonRegistry
from orch.core import store
from orch.core.ops import Ops

ORIGIN = {"origin": "http://testserver"}
OVER = {"capabilities": ["provider", "page", "settings", "decisions"],
        "actions": [{"id": "sync", "label": "Sync"}, {"id": "import", "label": "Import", "tickets": True}]}


class Addon:
    def __init__(self):
        self.providers, self.calls = [], []
        self.ticket = None
        self.result = None

    def widgets(self, slot, view):
        return []

    def decisions(self, view):
        return [PendingDecision("d1", "Approve from phone", ticket=self.ticket)]

    def resolve(self, decision_id, choice, ctx):
        self.calls.append(("resolve", decision_id, choice, ctx))
        return self.result

    def act(self, action_id, target, ctx):
        self.calls.append(("act", action_id, target, ctx))
        return self.result


@pytest.fixture
def addon(ws):
    obj = Addon()
    ws._addons = AddonRegistry(ws, {"demo": loaded(ws, obj, **OVER)})
    return obj


@pytest.fixture
def client(ws, addon, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    return c


def _decide(client, choice="apply"):
    return client.post("/addons/demo/decisions", data={"id": "d1", "choice": choice}, headers=ORIGIN,
                       follow_redirects=False).headers["location"]


def _act(client, action, target):
    return client.post(f"/addons/demo/actions/{action}", data={"target": target}, headers=ORIGIN,
                       follow_redirects=False).headers["location"]


def _reachable(root, limit=20000):
    """Every object reachable from `root` through attributes, slots, containers and properties' backing fields."""
    seen, stack, out = set(), [root], []
    while stack and len(seen) < limit:
        obj = stack.pop()
        if id(obj) in seen or isinstance(obj, (str, bytes, int, float, bool, type(None), type)):
            continue
        seen.add(id(obj))
        out.append(obj)
        if isinstance(obj, dict):
            stack.extend(obj.keys()); stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        if hasattr(obj, "__dict__") and not callable(obj):
            stack.extend(vars(obj).values())
        for cls in type(obj).__mro__:
            for slot in getattr(cls, "__slots__", ()):
                if hasattr(obj, slot):
                    stack.append(getattr(obj, slot))
    return out


def test_no_ops_ever_reaches_addon_code(client, addon, put):
    addon.ticket = put("backlog")
    _decide(client)
    _act(client, "sync", "a/b#1")
    assert [c[0] for c in addon.calls] == ["resolve", "act"]
    from orch.addons.api import AddonContext
    from orch.core.workspace import Workspace
    for call in addon.calls:
        graph = [o for arg in call for o in _reachable(arg)]
        assert any(isinstance(o, Workspace) for o in graph) and any(isinstance(o, AddonContext) for o in graph)
        assert [o for o in graph if isinstance(o, Ops)] == [], call[0]


def test_an_intent_for_another_ticket_is_refused(client, addon, put, ws):
    addon.ticket = put("backlog")
    other = put("backlog")
    addon.result = Intent("move", ref=other, value="open")
    assert "err=" in _decide(client) and "nothing+changed" in _decide(client)
    assert store.load(ws, other)[1].status == "backlog"
    addon.result = Intent("move", ref=other, value="open")
    assert "err=" in _act(client, "sync", addon.ticket)  # an action may only touch its own target
    assert store.load(ws, other)[1].status == "backlog"


def test_approve_needs_the_hash_the_human_saw(client, addon, put, ws):
    from orch.core.gates import gate_hash
    from orch.core.events import read_events
    tid = put("backlog", sections={"Requirements": "Do it.", "Acceptance criteria": "- works"})
    addon.ticket = tid
    addon.result = Intent("approve", ref=tid, gate="requirements", expected_hash="0" * 64)
    assert "err=" in _decide(client) and "changed+since+you+opened" in _decide(client)
    assert store.load(ws, tid)[1].status == "backlog"
    addon.result = Intent("approve", ref=tid, gate="requirements")
    assert "expected_hash" in _decide(client)
    addon.result = Intent("approve", ref=tid, gate="requirements", expected_hash=gate_hash(store.load(ws, tid)[1], "requirements"))
    assert "Approved" in _decide(client)
    t = store.load(ws, tid)[1]
    assert t.status == "open"
    approved = [e for e in read_events(ws) if e.kind == "gate.approved"][-1]
    assert approved.actor == "human:you" and approved.via == "dashboard"
    assert read_events(ws)[-1].kind == "addon.decision" and read_events(ws)[-1].data["intent"] == "approve"


def test_import_applies_a_known_type_and_priority_only(client, addon, ws):
    """GI-01: an imported chore must not become a feature; unknown values and epics fall back to the defaults."""
    addon.ticket = None
    for key, data in (("GH-1", {"type": "chore", "priority": "high"}), ("GH-2", {"type": "epic", "priority": "bogus"})):
        addon.result = Intent("import", ref=key, value=f"Issue {key}", data=data)
        assert "Imported" in _act(client, "import", key)
    metas = {x["external"][0]["key"]: x for x in (e.meta for e in store.scan(ws)) if x.get("external")}
    assert (metas["GH-1"]["type"], metas["GH-1"]["priority"]) == ("chore", "high")
    assert (metas["GH-2"]["type"], metas["GH-2"]["priority"]) == ("feature", "normal")


def test_ticket_intents_only_for_actions_declared_with_tickets(client, addon, ws):
    addon.ticket = None
    addon.result = Intent("import", ref="GH-7", value="Imported issue")
    assert "tickets" in _act(client, "sync", "GH-7")  # undeclared: refused
    assert "Imported+GH-7" in _act(client, "import", "GH-7")
    assert "Imported+GH-7" in _act(client, "import", "GH-7")  # idempotent by key
    assert len([e for e in store.scan(ws) if any(x.get("key") == "GH-7" for x in e.meta.get("external") or [])]) == 1
    addon.result = Intent("close", ref="GH-7")
    assert "needs+a+reason" in _act(client, "import", "GH-7")
    addon.ticket = "L-0001"
    addon.result = Intent("import", ref="L-0001", value="x")
    assert "tickets" in _decide(client)  # decisions never get close/reopen/import


@pytest.mark.parametrize("action", ["sync", "import"])
def test_actions_may_not_return_decision_intents(client, addon, put, ws, action):
    from orch.core.gates import gate_hash
    tid = put("backlog", sections={"Requirements": "Do it.", "Acceptance criteria": "- works"})
    h = gate_hash(store.load(ws, tid)[1], "requirements")
    for intent in (Intent("approve", ref=tid, gate="requirements", expected_hash=h), Intent("move", ref=tid, value="open"),
                   Intent("verdict", ref=tid, value="pass"), Intent("request_changes", ref=tid, gate="requirements",
                                                                   reason="x"),
                   Intent("answer", ref=tid, qid="Q1", value="yes")):
        addon.result = intent
        loc = _act(client, action, tid)
        assert "err=" in loc and "only+from+a+decision" in loc, intent.kind
    assert store.load(ws, tid)[1].status == "backlog"


def test_strings_and_none_change_nothing(client, addon, put, ws):
    addon.ticket = put("backlog")
    addon.result = "Noted"
    assert "Noted" in _decide(client)
    addon.result = 42
    assert "not+an+Intent" in _decide(client)
    assert store.load(ws, addon.ticket)[1].status == "backlog"


@pytest.mark.parametrize("kw", [{"kind": "delete"}, {"kind": "move", "ref": 7}, {"kind": "none", "reason": "x" * 2001},
                                {"kind": "none", "data": []}])
def test_intent_validates_itself(kw):
    with pytest.raises(ValueError):
        Intent(**kw)


def test_import_logs_the_reason(client, addon, ws):
    addon.result = Intent("import", ref="GH-7", value="Imported issue",
                          reason="Imported from https://github.com/a/b/issues/7")
    assert "Imported+GH-7" in _act(client, "import", "GH-7")
    t = store.load(ws, "GH-7")[1]
    assert "Imported from https://github.com/a/b/issues/7" in t.section("Log")


def test_close_and_reopen_intents_run_as_the_human(client, addon, ws):
    from orch.core.events import read_events
    addon.result = Intent("import", ref="GH-7", value="Imported issue")
    _act(client, "import", "GH-7")
    addon.result = Intent("close", ref="GH-7", reason="GH-7 is closed in GitHub")
    assert "err=" in _act(client, "sync", "GH-7")  # undeclared: refused
    assert store.load(ws, "GH-7")[1].status == "backlog"
    addon.result = Intent("close", ref="GH-8", reason="x")
    assert "nothing+changed" in _act(client, "import", "GH-7")  # another ref than the target
    addon.result = Intent("close", ref="GH-7", reason="GH-7 is closed in GitHub")
    assert "Closed+L-0001" in _act(client, "import", "GH-7")
    moved = [e for e in read_events(ws) if e.kind == "ticket.moved"][-1]
    assert moved.actor == "human:you" and moved.via == "dashboard" and moved.data["reason"] == "GH-7 is closed in GitHub"
    assert read_events(ws)[-1].data["intent"] == "close"
    addon.result = Intent("reopen", ref="GH-7", reason="GH-7 is open again")
    assert "Reopened+L-0001+to+backlog" in _act(client, "import", "GH-7")
    assert store.load(ws, "GH-7")[1].status == "backlog"
