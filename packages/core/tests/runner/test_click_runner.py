"""#1198 C14: dashboard execution on and off by click: the click signer's ``runner`` op. The work is the CLI's own
(``orch runner enable``, ``enablement.set_enabled``); the service manager is a fake through the host's seam."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orch.click.log import ClickLog
from orch.click.protocol import FEATURES, OPS, RUNNER_LINE, ClickError
from orch.click.signer import ClickSigner
from orch.host import cli as host_cli
from orch.host import enable as svc
from orch.runner import enablement
from tests.runner.test_runner_cli import WID, FakeManager, env  # noqa: F401 - fixture


@pytest.fixture
def manager(monkeypatch) -> FakeManager:
    fake = FakeManager()
    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", fake)
    monkeypatch.setattr(svc, "dev_source", lambda *a, **k: None)  # this test runs from a checkout
    return fake


@pytest.fixture
def signer(env, manager, monkeypatch):  # noqa: F811
    state = Path(env["ORCH_STATE_DIR"])
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    uid = os.getuid()
    entry = {"workspace_id": WID, "prefix": "ACME", "host_uid": uid, "root": "/w/acme"}
    s = ClickSigner(
        state,
        uid=uid,
        connect=lambda e: pytest.fail("the runner switch never connects to the host"),
        workspaces=lambda: [entry],
        log=ClickLog(state),
    )
    yield s, state, entry
    s.close()


def review(s, on, **kw):
    return s.handle("runner", {"workspace_id": WID, "review": True, "on": on, **kw})


def act(s, on, **kw):
    got = review(s, on, **kw)
    return s.handle("runner", {"workspace_id": WID, "on": on, "view_digest": got["view_digest"], **kw})


def test_the_signer_advertises_the_runner_switch():
    assert "runner" in OPS and "runner_switch" in FEATURES


def test_the_review_says_what_turning_it_on_allows_and_changes_nothing(signer):
    s, state, _ = signer
    got = review(s, True)
    assert RUNNER_LINE in got["view"]["lines"] and enablement.CARD_TEXT in got["view"]["lines"]
    assert got["view"]["confirm"] == {"kind": "click"} and got["view"]["terminals"] is False
    assert enablement.state_of(state, WID, os.getuid())["enabled"] is False


def test_on_then_off_is_the_cli_switch(signer, manager):
    s, state, _ = signer
    assert act(s, True, terminals=True) == {"workspace_id": WID, "enabled": True, "terminals": True}
    assert enablement.state_of(state, WID, os.getuid()) == {"enabled": True, "terminals": True}
    entry = enablement.read(state, os.getuid())["workspaces"][WID]
    assert RUNNER_LINE not in entry["text"] and enablement.CARD_TEXT in entry["text"]  # the CLI's own risk text
    assert manager.ran, "the runner service was installed through the host's manager"
    assert act(s, False) == {"workspace_id": WID, "enabled": False, "terminals": False}
    assert enablement.state_of(state, WID, os.getuid())["enabled"] is False


def test_terminals_are_off_unless_asked_for(signer):
    s, state, _ = signer
    assert act(s, True) == {"workspace_id": WID, "enabled": True, "terminals": False}  # no `terminals` sent
    assert enablement.state_of(state, WID, os.getuid())["terminals"] is False


def test_the_off_review_says_open_terminals_close(signer):
    s, _, _ = signer
    from orch.click.protocol import RUNNER_OFF_LINE

    assert "Open terminals close within a second" in RUNNER_OFF_LINE
    assert RUNNER_OFF_LINE in review(s, False)["view"]["lines"]


def test_on_without_terminals(signer):
    s, state, _ = signer
    assert act(s, True, terminals=False)["terminals"] is False
    assert enablement.NO_TERMINALS_TEXT in enablement.read(state, os.getuid())["workspaces"][WID]["text"]


def test_a_digest_of_another_review_changes_nothing(signer):
    s, state, _ = signer
    stale = review(s, True, terminals=False)["view_digest"]
    with pytest.raises(ClickError) as info:  # the person read the no-terminals review; this asks for terminals
        s.handle("runner", {"workspace_id": WID, "on": True, "terminals": True, "view_digest": stale})
    assert info.value.code == "invalid.input"
    with pytest.raises(ClickError):
        s.handle("runner", {"workspace_id": WID, "on": True})
    assert enablement.state_of(state, WID, os.getuid())["enabled"] is False


def test_off_needs_the_off_review(signer):
    s, state, _ = signer
    act(s, True)
    on_digest = review(s, True)["view_digest"]
    with pytest.raises(ClickError):
        s.handle("runner", {"workspace_id": WID, "on": False, "view_digest": on_digest})
    assert enablement.state_of(state, WID, os.getuid())["enabled"] is True


def test_the_cli_refusals_come_through_with_their_codes(signer, monkeypatch):
    s, state, _ = signer
    monkeypatch.setattr(svc, "dev_source", lambda *a, **k: "this orch is a source checkout")
    with pytest.raises(ClickError) as info:
        act(s, True)
    assert info.value.code == "invalid.input" and "source checkout" in info.value.message
    assert enablement.state_of(state, WID, os.getuid())["enabled"] is False


def test_a_host_running_as_another_user_is_terminal_only(signer):
    s, _, entry = signer
    entry["host_uid"] = os.getuid() + 1
    with pytest.raises(ClickError) as info:
        review(s, True)
    assert info.value.code == "custody.terminal_needed"
    assert "orch runner enable|disable" in info.value.message and "convenience" not in info.value.message


@pytest.mark.parametrize("bad", [{"on": "yes"}, {"on": True, "terminals": 1}, {"on": True, "extra": 1}, {}])
def test_strict_arguments(signer, bad):
    s, _, _ = signer
    with pytest.raises(ClickError) as info:
        s.handle("runner", {"workspace_id": WID, "review": True, **bad})
    assert info.value.code == "invalid.input"
