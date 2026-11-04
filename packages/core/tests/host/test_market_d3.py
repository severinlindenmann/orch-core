"""D3: staged bytes and required settings, one review, grant last."""
# ruff: noqa: F811

import pytest

from orch import canon
from orch.host import marketinstall, sequence
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.human_helpers import err, ok
from tests.host.test_sequence import Rig

lab, served, human, net = human_helpers.lab, human_helpers.served, human_helpers.human, M.net
SPEC = {
    "type": "object",
    "properties": {"channel": {"type": "string", "maxLength": 80, "default": "general"}},
    "required": ["channel"],
    "additionalProperties": False,
}


def setup(served, net, *, settings=True):
    extra = {"settings": {"title": "Options", "schema": SPEC}} if settings else {}
    net.add(pkg=M.files(**extra))
    M.add_market(served, net)
    st = ok(M.stage(served))["data"]
    return st, {
        "v": 1,
        "kind": "addon_enable",
        "name": st["name"],
        "staged": st["stage"],
        "grant": st["grant"],
        "settings": {"addons": {st["name"]: {"channel": "general"}}} if settings else {},
    }


@pytest.mark.parametrize("settings", [True, False])
def test_sequence_installs_verified_bytes_and_turns_on(served, human, net, tmp_path, settings):
    st, intent = setup(served, net, settings=settings)
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    assert not M.addon_dir(served).exists()
    out = rig.driver().run(first)
    assert out["landed"] == (2 if settings else 1)
    assert [e["type"] for _, events, _ in rig.submitted for e in events] == (
        ["settings.changed", "addon.granted"] if settings else ["addon.granted"]
    )
    assert M.mfiles(served).read_stage(st["stage"]) is None
    assert marketinstall.state_of(served.service, "standup") == "enabled"
    assert rig.submitted[-1][1][0]["staged"] == st["stage"]


def test_kill_after_settings_installs_nothing(served, human, net, tmp_path):
    _, intent = setup(served, net)
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    signed = human.sign(first)
    ok(human.submit_res(first["request"]["id"], signed.events))
    assert not M.addon_dir(served).exists()
    assert M.mfiles(served).installs()["addons"] == {}
    assert signed.events[0]["set"] == intent["settings"]


def test_extra_signed_capability_refused_before_prefix(served, human, net, tmp_path):
    st, intent = setup(served, net)
    intent["grant"]["capabilities"].append("serve_http")
    rig = Rig(human, tmp_path, intent)
    op, args = sequence.step_args(intent, 0, None)
    assert (
        err(human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer, "sequence": rig.meta0()}))
        == "addon.stage_changed"
    )
    assert M.mfiles(served).read_stage(st["stage"]) is None
    assert not M.addon_dir(served).exists()


def test_stage_swap_after_verified_read_uses_snapshot(served, human, net, monkeypatch):
    st, _ = setup(served, net, settings=False)
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    signed = human.sign(prepared)
    converge = marketinstall._converge

    def swap(service, stage, ev, **kwargs):
        M.mfiles(served).drop_stage(stage)
        return converge(service, stage, ev, **kwargs)

    monkeypatch.setattr(marketinstall, "_converge", swap)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    from orch.addons.package import read_package

    assert read_package(M.addon_dir(served)).digest == st["package_sha256"]


def test_token_never_reaches_event_log(served, human, net, tmp_path):
    _, intent = setup(served, net)
    # A syntactically token-shaped demo, never a real credential.
    token = "ghp_" + "a" * 36
    intent["settings"]["addons"]["standup"]["channel"] = token
    with pytest.raises(sequence.SequenceInvalid):
        Rig(human, tmp_path, intent)
    assert token not in canon.dumps(dict(served.service.store.state.workspace.settings)).decode()
    for path in M.addon_dir(served).parent.parent.rglob("*.jsonl"):
        assert token not in path.read_text()


def test_already_on_refuses_before_settings(served, human, net, tmp_path):
    _, intent = setup(served, net)
    rig = Rig(human, tmp_path, intent)
    rig.driver().run(rig.first())
    # Re-stage the same package for a new proposed setup, while it is running.
    with pytest.raises(AssertionError, match="already on"):
        Rig(human, tmp_path / "again", intent).first()


def test_signer_view_has_inline_values_identity_and_capability_risk(served, net):
    from tests.host.test_click import Click

    st, intent = setup(served, net)
    c = Click(served)
    try:
        meta = {"step": 0, "of": 2, "intent": intent, "digest": sequence.digest_of(served.wid, c.owner.ref, intent)}
        op, args = sequence.step_args(intent, 0, None)
        prepared = c.prepare(op, args, sequence=meta)
        view = c.view(prepared["request_id"])["view"]
        assert view["headline"] == "Install and turn on add-on"
        assert any("internet" in line or "network" in line for line in view["risk_lines"])
        content = view["content"]
        assert "summary" not in content
        rows = {r["field"]: r["after"] for r in content["fields_changed"]}
        assert rows["standup: channel"] == '"general"'
        assert rows["add-on"] == "standup" and rows["version"] == st["version"]
        assert rows["publisher (per host, not signed)"] == st["market"]["publisher"]["fingerprint"]
        assert any(st["package_sha256"] in line for line in view["lines"])
        # A prepared settings prefix with signed capability risk never signs merely by dwelling.
        assert op == "settings.set"
        assert view["risk_lines"] == ["It may use the internet."]
        # the relay/dashboard contract 4 check: risk lines are empty exactly for the one-press allowlist, so this staged
        # sequence (first step settings.set, capability risk) is a press-mode View that never arms and never auto-signs
        from orch.click import protocol

        assert (view["risk_lines"] == []) == (view["op"] in protocol.ONE_PRESS_OPS) and view["risk_lines"]
        c.mono.t += 5
        assert c.view(prepared["request_id"])["view"]["risk_lines"] == view["risk_lines"]
        assert c.signatures == 0 and not M.addon_dir(served).exists()
    finally:
        c.close()


def test_becoming_on_after_review_refuses_settings_prefix(served, human, net, tmp_path):
    _, intent = setup(served, net)
    first = Rig(human, tmp_path, intent).first()
    ok(human.submit_res(first["request"]["id"], human.sign(first).events))
    changed = {**intent, "settings": {"addons": {"standup": {"channel": "changed"}}}}
    prefix = Rig(human, tmp_path / "other", changed).first()
    signed = human.sign(prefix)
    op, args = sequence.step_args(intent, 1, None)
    grant = human.prepare(op, args)
    ok(human.submit_res(grant["request"]["id"], human.sign(grant).events))
    assert err(human.submit_res(prefix["request"]["id"], signed.events)) == "invalid.input"
    assert served.service.store.state.workspace.settings["addons"]["standup"]["channel"] == "general"


@pytest.mark.parametrize(
    "secret_schema",
    [
        {"type": "secret", "default": "demo-not-a-real-secret"},
        {"type": "string", "maxLength": 80, "format": "password", "default": "demo-not-a-real-secret"},
        {"type": "string", "maxLength": 80, "writeOnly": True},
    ],
)
def test_secret_typed_fields_cannot_be_staged(served, net, secret_schema):
    spec = {**SPEC, "properties": {"channel": secret_schema}}
    net.add(pkg=M.files(settings={"title": "Options", "schema": spec}))
    M.add_market(served, net)
    assert not M.stage(served)["ok"]
    assert M.mfiles(served).stages() == []
    assert not M.addon_dir(served).exists()


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_refusal_consumes_stage_before_prefix(served, human, net, tmp_path, cancel):
    st, intent = setup(served, net)
    first = Rig(human, tmp_path, intent).first()
    rid = first["request"]["id"]
    if cancel:
        ok(human.c.req("human.cancel", {"request_id": rid}))
    else:
        signed = human.sign(first)
        signed.events[0]["sig"] = "bad"
        assert not human.submit_res(rid, signed.events)["ok"]
    assert M.mfiles(served).read_stage(st["stage"]) is None
    assert not M.addon_dir(served).exists()
    assert not served.service.store.state.workspace.settings.get("addons")


@pytest.mark.parametrize("missing", ["digest", "caps"])
def test_staged_grant_requires_reviewed_digest_and_caps(served, human, net, missing):
    st, intent = setup(served, net, settings=False)
    args = sequence.steps(intent)[0]["args"]
    del args[missing]
    assert err(human.prepare_res("addon.grant", args)) == "invalid.input"
    assert not M.addon_dir(served).exists()


@pytest.mark.parametrize("missing", ["package_sha256", "capabilities"])
def test_staged_intent_requires_digest_and_caps(served, net, missing):
    _, intent = setup(served, net)
    del intent["grant"][missing]
    with pytest.raises(sequence.SequenceInvalid):
        sequence.validate_intent(intent)


@pytest.mark.parametrize("sweep", [True, False])
def test_stage_ttl_is_thirty_minutes_and_expiry_deletes_file(served, net, sweep):
    from orch.market import MarketError

    st, _ = setup(served, net)
    files = M.mfiles(served)
    doc = files.read_stage(st["stage"])
    assert doc["expires_at"] - doc["created_at"] == 30 * 60
    path = files.dir / "staged" / f"{st['stage']}.json"
    served.lab.ws.clock[0] = doc["expires_at"]
    if sweep:
        M.on_worker(served, lambda: marketinstall.recover(served.service))
    else:
        with pytest.raises(MarketError):
            marketinstall.staged_package(files, st["name"], st["stage"], doc["expires_at"])
    assert not path.exists()


@pytest.mark.parametrize("code, retained", [("lock.busy", True), ("invalid.input", False)])
def test_submit_refusal_retains_only_retryable_stage(served, human, net, tmp_path, monkeypatch, code, retained):
    from orch.host.errors import HostError
    from orch.host.human import HumanOps

    st, intent = setup(served, net)
    first = Rig(human, tmp_path, intent).first()
    signed = human.sign(first)
    original = HumanOps._submit

    def refuse(*args, **kwargs):
        raise HostError(code, "test refusal")

    monkeypatch.setattr(HumanOps, "_submit", refuse)
    assert err(human.submit_res(first["request"]["id"], signed.events)) == code
    assert (M.mfiles(served).read_stage(st["stage"]) is not None) == retained
    if retained:
        monkeypatch.setattr(HumanOps, "_submit", original)
        ok(human.submit_res(first["request"]["id"], signed.events))
