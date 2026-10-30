"""Settings and the grant are one server-built, workspace-only intent."""

from types import SimpleNamespace

import pytest
from orch.addons.settings_needs import preview_grant
from orch.host import sequence
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.errors import HostError
from orch_dashboard.signing import PrepareBody, expected, host_call, host_files, plan

PUBLISH = {"server": "https://files.example.test", "share": "https://links.example.test"}


def parse(**fields):
    return TypeAdapter(PrepareBody).validate_python({"action": "addon_enable", **fields})


@pytest.mark.parametrize("name,fields", [("publish", {"publish": PUBLISH})])
def test_first_step_and_expected(name, fields):
    body = parse(name=name, **fields)
    assert host_call(body) == ("settings.set", {"set": fields})
    assert host_files(body) is None
    check = expected(body)
    assert check([{"type": "settings.changed", "set": fields}])
    assert not check([{"type": "settings.changed", "set": {}}])
    assert not check([{"type": "settings.changed", "set": fields}] * 2)


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "publish", "publish": PUBLISH, "grant": {}},
        {"name": "publish", "models": {"codex": {"work": "gpt-5.4"}}},
        {"name": "model-routing", "publish": PUBLISH},  # no longer a built-in add-on
        {"name": "publish", "publish": PUBLISH, "models": {"codex": {"work": "gpt-5.4"}}},
        {"name": "publish"},
        {"name": "publish", "publish": {**PUBLISH, "extra": "x"}},
    ],
)
def test_invalid_input(fields):
    with pytest.raises(ValidationError):
        parse(**fields)


def test_one_address_for_files_and_links_is_accepted():
    """The form's default and what `orch install` signs (server = share = url): equal addresses are valid."""
    one = {"server": "https://pub.example.test", "share": "https://pub.example.test"}
    body = parse(name="publish", publish=one)
    assert host_call(body) == ("settings.set", {"set": {"publish": one}})


class Link:
    def __init__(self):
        self.welcome = {"peer": {"person": "p_" + "1" * 32}, "ops": {"settings.set": 1, "addon.preview": 1}}
        self.state, self.head = "not_granted", None

    def call(self, op, args=None):
        if op == "addon.list":
            return {"addons": [{"name": "publish", "state": self.state}]}
        assert op == "activity.list"
        return {
            "items": []
            if self.head is None
            else [{"type": "addon.disabled", "id": self.head, "data": {"name": "publish"}}]
        }


def test_plan_uses_preview_and_latest_switch(tmp_path):
    link = Link()
    grant = preview_grant(tmp_path, "publish")
    reads = []

    def call(ws, op, args):
        reads.append((op, args))
        assert op == "addon.preview" and args == {"name": "publish"}
        return {"grant": grant}

    gateway = SimpleNamespace(link=lambda ws: link, call=call)
    body = parse(name="publish", publish=PUBLISH)
    ws = "a" * 32
    first = plan(gateway, ws, body)
    intent = body.intent(grant)
    digest = sequence.digest_of(ws, link.welcome["peer"]["person"], intent)
    assert first.sequence == {"step": 0, "of": 2, "digest": digest, "intent": intent}
    assert intent["grant"] == grant and first.files is None
    assert first.base == {"sequence": digest, "addon": {"addon": "publish", "head": None}}
    assert plan(gateway, ws, body).idem(ws) == first.idem(ws)
    link.state = "active"
    with pytest.raises(HostError) as err:
        plan(gateway, ws, body)
    assert err.value.error["code"] == "addon.already"
    link.state, link.head = "disabled", "second-switch"
    assert plan(gateway, ws, body).idem(ws) != first.idem(ws)
    link.welcome["ops"].pop("addon.preview")
    with pytest.raises(HostError) as err:
        plan(gateway, ws, body)
    assert err.value.error["code"] == "op.unsupported"


@pytest.mark.parametrize(
    "settings,want",
    [
        ({"publish": PUBLISH}, True),
        ({"publish": None}, False),
        (None, None),
    ],
)
def test_rows_report_setup(client, token, stub, gateway, monkeypatch, settings, want):
    from test_addon_switches import _get, _VersionLink

    monkeypatch.setattr(gateway, "enabled", lambda ws: _VersionLink())
    reads = []

    def call(ws, op, args=None):
        if op == "whoami":
            return {"runner": None}
        assert op == "workspace.settings"
        reads.append(op)
        if settings is None:
            raise RuntimeError("unavailable")
        return settings

    monkeypatch.setattr(gateway, "call", call)
    rows = {r["id"]: r for r in _get(client, token, stub, "add-ons").json()["add_ons"]}
    assert rows["publish"]["needs"] == "publish" and rows["publish"]["set_up"] is want
    assert "model-routing" not in rows
    assert rows["quick"]["needs"] is None and rows["quick"]["set_up"] is True
    assert rows["usage"]["needs"] is None and rows["usage"]["set_up"] is True
    assert reads == ["workspace.settings"]


@pytest.mark.parametrize(
    "state,want",
    [
        ({"parts": {"publish": {"url": "http://127.0.0.1:8102"}}}, "http://127.0.0.1:8102"),
        ({"parts": {"publish": {"url": "https://pub.example.org"}}}, "https://pub.example.org"),
        ({"parts": {"publish": {"url": "http://example.com"}}}, None),  # not an address core accepts
        ({"parts": {"publish": {"url": "https://pub.example.org/x"}}}, None),
        ({"parts": {"publish": {}}}, None),
        ({"parts": {}}, None),
        (None, None),  # no installer state at all (Publish hosted elsewhere)
    ],
)
def test_rows_suggest_the_publish_server_installed_here(
    client, token, stub, gateway, monkeypatch, tmp_path, state, want
):
    import json

    from test_addon_switches import _get, _VersionLink

    (tmp_path / "install").mkdir()
    if state is not None:
        (tmp_path / "install" / "install.json").write_text(json.dumps(state))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(gateway, "enabled", lambda ws: _VersionLink())
    monkeypatch.setattr(
        gateway, "call", lambda ws, op, args=None: {"runner": None} if op == "whoami" else {"publish": None}
    )
    rows = {r["id"]: r for r in _get(client, token, stub, "add-ons").json()["add_ons"]}
    assert rows["publish"]["suggest"] == want
    assert "suggest" not in rows["quick"] or rows["quick"]["suggest"] is None


@pytest.mark.parametrize("name,fields", [("publish", {"publish": PUBLISH})])
def test_plan_reads_the_preview_over_the_real_host_link(gateway, stub, p1, name, fields):
    """e2e run 4: the grant body is read over the real `HostLink` (no stubbed `gateway.call`), answered by core's own
    `addon.preview` handler. `addon.preview` must be a read there: a call it treats as a write needs an idem, and the
    prepare answered 500."""
    body = parse(name=name, **fields)
    planned = plan(gateway, stub.ws, body)
    grant = preview_grant(p1.store_dir, name)
    assert planned.sequence["intent"] == body.intent(grant)
    assert planned.sequence["intent"]["grant"]["name"] == name
    assert planned.base["addon"] == {"addon": name, "head": None}


def test_the_model_choice_is_offered_by_the_choose_model_capability_not_by_the_name(gateway, stub, monkeypatch):
    class Link:
        welcome = {"workspace": {"id": "w_cap"}}

        def has(self, cap):
            return False

        def supports(self, op, cap):
            return True

        def call(self, op, args=None, **kw):
            return {"addons": self.rows}

    link = Link()
    monkeypatch.setattr(gateway, "enabled", lambda ws: link)
    link.rows = [{"name": "my-picker", "state": "active", "capabilities": ["choose_model"]}]
    assert "model_routing" in gateway.features(stub.ws)
    for rows in (
        [{"name": "model-routing", "state": "active", "capabilities": ["network"]}],  # the name alone decides nothing
        [{"name": "model-routing", "state": "active"}],
        [{"name": "my-picker", "state": "disabled", "capabilities": ["choose_model"]}],
        [],
    ):
        link.rows = rows
        assert "model_routing" not in gateway.features(stub.ws)
