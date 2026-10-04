"""AI Factory CLI (#2): `orch approve <epic> --factory` and `orch permit request|list|grant|deny|revoke`."""
import json

import pytest

from orch import actor
from orch.cli import run


@pytest.fixture
def switch(monkeypatch, configure):
    configure(factory={"enabled": True})

    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


def _run(capsys, *args):
    code = run(list(args))
    return code, capsys.readouterr()


def _ok(capsys, *args):
    code, out = _run(capsys, *args)
    assert code == 0, (args, out)
    return out.out


def _new(capsys, *args):
    tid = json.loads(_ok(capsys, "new", *args, "--json"))["id"]
    _ok(capsys, "section", "set", tid, "Requirements", "-m", "r")
    _ok(capsys, "section", "set", tid, "Acceptance criteria", "-m", "- [ ] a")
    return tid


def test_factory_flow_in_the_terminal(capsys, switch):
    eid = _new(capsys, "-t", "Billing", "--type", "epic")
    switch.human(eid)
    out = _ok(capsys, "approve", eid, "requirements", "--factory")
    assert "AI Factory: on" in out and "25 children" in out and "72" in out
    switch.agent()
    cid = _new(capsys, "-t", "child", "--epic", eid)
    _ok(capsys, "section", "set", cid, "Plan", "-m", "1. do it")
    _ok(capsys, "epic", "auto-approve", cid)
    rid = json.loads(_ok(capsys, "permit", "request", "make deploy-staging", "--ticket", cid,
                         "--reason", "Denied by auto mode", "--json"))["id"]
    assert rid.startswith("P-")
    code, _ = _run(capsys, "permit", "grant", rid)
    assert code != 0  # an agent never grants
    switch.human(rid)
    out = _ok(capsys, "permit", "grant", rid, "--for-epic")
    assert "make deploy-staging" in out and "granted (epic)" in out
    switch.agent()
    listed = json.loads(_ok(capsys, "permit", "list", "--json"))
    assert listed["requests"] == [] and listed["grants"][0]["live"] is True
    gid = listed["grants"][0]["grant"]
    switch.human(gid)
    _ok(capsys, "permit", "revoke", gid)
    switch.agent()
    assert json.loads(_ok(capsys, "permit", "list", "--json"))["grants"][0]["why"] == "revoked"


def test_wrong_confirmation_grants_nothing(capsys, switch):
    eid = _new(capsys, "-t", "Billing", "--type", "epic")
    switch.human(eid)
    _ok(capsys, "approve", eid, "requirements", "--factory")
    switch.agent()
    cid = _new(capsys, "-t", "child", "--epic", eid)
    _ok(capsys, "section", "set", cid, "Plan", "-m", "1. do it")
    _ok(capsys, "epic", "auto-approve", cid)
    rid = json.loads(_ok(capsys, "permit", "request", "make e2e", "--ticket", cid, "--json"))["id"]
    switch.human("P-999")
    code, _ = _run(capsys, "permit", "grant", rid)
    assert code != 0
    switch.agent()
    assert json.loads(_ok(capsys, "permit", "list", "--json"))["grants"] == []
