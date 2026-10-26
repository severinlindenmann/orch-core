"""B6 (owner decision F): the workspace setting ``sign_methods`` (the ways a signature may be made), its validation
(terminal always, touchid reserved), its effect on convenience signing, and the risk acknowledgement at enable time."""

from __future__ import annotations

import time

import pytest

from orch.click.protocol import op_class
from orch.host import convenience, reads_ws
from orch.model import Refusal
from orch.schema import SchemaError, validate_settings
from tests.host import human_helpers
from tests.host.human_helpers import err

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.mark.parametrize(
    "good", [["terminal"], ["terminal", "click"], ["phone", "terminal"], ["terminal", "click", "phone"]]
)
def test_sign_methods_accepts_terminal_with_click_and_phone(good):
    validate_settings({"sign_methods": good})


@pytest.mark.parametrize(
    "bad",
    [
        [],
        ["click"],
        ["phone", "click"],
        ["terminal", "touchid"],
        ["terminal", "terminal"],
        ["terminal", "key"],
        "terminal",
        None,
    ],
)
def test_sign_methods_refuses_no_terminal_touchid_duplicates_and_unknown(bad):
    with pytest.raises(SchemaError):
        validate_settings({"sign_methods": bad})


def test_the_owner_sets_sign_methods_through_a_signed_settings_change(human, served):
    _, res, signed = human.run("settings.set", {"set": {"sign_methods": ["phone", "terminal"]}})
    assert signed.events[0]["set"] == {"sign_methods": ["phone", "terminal"]}
    ws = served.lab.ws.other()
    try:
        assert list(ws.state.workspace.settings["sign_methods"]) == ["terminal", "phone"]  # the canonical order
        assert reads_ws.sign_methods(served.service, ws.state.workspace) == ["terminal", "phone"]
    finally:
        ws.close()
    # a request without terminal never reaches the signer
    assert err(human.prepare_res("settings.set", {"set": {"sign_methods": ["click"]}})) == "invalid.input"
    assert err(human.prepare_res("settings.set", {"set": {"sign_methods": ["terminal", "touchid"]}})) == "invalid.input"


def test_an_unset_sign_methods_reads_as_null_not_a_narrower_default(served):
    """MEDIUM-2: nothing set it, so the model judges no way (every way is allowed) and the read says "not set"."""
    ws = served.lab.ws.other()
    try:
        state = ws.state.workspace
        assert reads_ws.sign_methods(served.service, state) is None
        convenience.set_enabled(served.service.state_dir, served.service.workspace_id, True, served.service.host_uid)
        assert reads_ws.sign_methods(served.service, state) is None
    finally:
        ws.close()


def test_a_workspace_that_excludes_click_refuses_convenience_signing(human, served):
    svc = served.service
    person = human.person.ref

    def why():
        svc.store.refresh()
        return convenience.reason(svc.store, svc.state_dir, svc.workspace_id, svc.host_uid, person, human.person.device)

    human.run("settings.set", {"set": {"sign_methods": ["terminal", "phone"]}})
    convenience.set_enabled(svc.state_dir, svc.workspace_id, True, svc.host_uid)
    assert why() == "disabled"
    convenience.set_enabled(svc.state_dir, svc.workspace_id, False, svc.host_uid)
    human.run("settings.set", {"set": {"sign_methods": ["terminal", "click"]}})
    convenience.set_enabled(svc.state_dir, svc.workspace_id, True, svc.host_uid)
    assert why() != "disabled"


def test_click_cannot_be_removed_while_the_key_is_in_convenience_custody(human, served):
    """HIGH-2 (2): the change would lock the honest signer out; the refusal names the way back."""
    svc = served.service
    convenience.set_enabled(svc.state_dir, svc.workspace_id, True, svc.host_uid)
    res = human.prepare_res("settings.set", {"set": {"sign_methods": ["terminal", "phone"]}})
    assert err(res) == "invalid.input"
    assert "orch keys custody passphrase" in res["error"]["hint"]
    human.prepare("settings.set", {"set": {"sign_methods": ["terminal", "click"]}})  # keeping click is fine
    convenience.set_enabled(svc.state_dir, svc.workspace_id, False, svc.host_uid)
    human.run("settings.set", {"set": {"sign_methods": ["terminal", "phone"]}})  # key back in passphrase custody


def test_custody_not_the_label_decides_while_click_is_left_out(human, served):
    """HIGH-2 (1): with the flag on (the key is in convenience custody) and click left out, an event lands from this
    machine's person path under no label, however it declares itself."""
    svc = served.service
    human.run("settings.set", {"set": {"sign_methods": ["terminal", "phone"]}})
    assert convenience.refuse_excluded_click_custody(svc) is None  # flag off: nothing to refuse
    convenience.set_enabled(svc.state_dir, svc.workspace_id, True, svc.host_uid)
    refused = convenience.refuse_excluded_click_custody(svc)
    assert refused is not None and refused.code == "sign.convenience_unavailable"
    from orch.host import human as host_human

    landed, error = host_human.append_signed(svc, "workspace", [], [], signed_by=None)
    assert landed == [] and error is not None and error.code == "sign.convenience_unavailable"


def test_a_sign_methods_change_is_a_click():
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"claim_ttl_min": 30}}]) == "click"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"grant_hours": 4}}]) == "click"  # B8
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"sign_methods": ["terminal"]}}]) == "click"


def test_switching_click_to_sign_on_records_the_risk_acknowledgement(served):
    svc = served.service
    assert convenience.risk_ack(svc.state_dir, svc.workspace_id, svc.host_uid) is None
    convenience.set_enabled(svc.state_dir, svc.workspace_id, True, svc.host_uid)
    stamp = convenience.risk_ack(svc.state_dir, svc.workspace_id, svc.host_uid)
    assert (
        stamp is not None and abs(time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) - time.time()) < 86400 + 7200
    )
    convenience.set_enabled(svc.state_dir, svc.workspace_id, False, svc.host_uid)
    assert convenience.risk_ack(svc.state_dir, svc.workspace_id, svc.host_uid) is None


def test_the_model_refuses_a_settings_change_without_terminal_even_when_the_schema_is_skipped():
    from orch.model.workspace import settings_changed

    class Ws:
        repos: dict = {}
        settings: dict = {}

    assert Refusal  # the schema is the first gate; the model repeats the rule for events from older cores
    ws = Ws()
    owner_only = lambda ws, e: None  # noqa: E731
    import orch.model.workspace as mw

    mw._owner_only, saved = owner_only, mw._owner_only
    try:
        refusal = settings_changed(ws, {"set": {"sign_methods": ["click"]}, "actor": {"id": "x"}})
    finally:
        mw._owner_only = saved
    assert refusal is not None and "terminal" in str(refusal)


def test_the_settings_read_carries_sign_methods(human, served):
    from tests.host.human_helpers import ok

    assert ok(human.c.req("workspace.settings", {}))["data"]["sign_methods"] is None
    human.run("settings.set", {"set": {"sign_methods": ["phone", "terminal", "click"]}})
    assert ok(human.c.req("workspace.settings", {}))["data"]["sign_methods"] == ["terminal", "click", "phone"]


def test_a_key_left_in_convenience_custody_counts_as_click_even_with_the_flag_off(monkeypatch, tmp_path):
    """B6 (Opus round 2): "Turn off" leaves the key in convenience custody; the person path must still be refused
    when sign_methods leaves out click, and an unreadable selection fails closed."""
    import types

    from orch.custody import select
    from orch.host import convenience

    svc = types.SimpleNamespace(
        state_dir=tmp_path, workspace_id="a" * 32, host_uid=501,
        store=types.SimpleNamespace(state=types.SimpleNamespace(workspace=types.SimpleNamespace(
            settings={"sign_methods": ["terminal"]}))),
    )  # fmt: skip
    monkeypatch.setattr(convenience, "enabled", lambda *a: False)
    monkeypatch.setattr(select, "read_selection", lambda *a: {"custody": "convenience"})
    assert convenience.refuse_excluded_click_custody(svc) is not None
    monkeypatch.setattr(select, "read_selection", lambda *a: {"custody": "passphrase", "transition": None})
    assert convenience.refuse_excluded_click_custody(svc) is None
    monkeypatch.setattr(
        select, "read_selection", lambda *a: {"custody": "passphrase", "transition": {"to": "convenience"}}
    )
    assert convenience.refuse_excluded_click_custody(svc) is not None

    def boom(*a):
        raise select.CustodyError("bad")

    monkeypatch.setattr(select, "read_selection", boom)
    assert convenience.refuse_excluded_click_custody(svc) is not None
