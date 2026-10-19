"""Turning on an add-on that needs settings, as ONE sequence (kind ``addon_enable``): ``settings.set`` then
``addon.grant``, on the workspace log. One test group per invariant AE1 to AE8 of
~/orch-dev/state/design-addon-enable-sequence.md."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from orch import canon
from orch.addons import settings_needs as needs
from orch.host import review, sequence, sequence_terminal
from orch.host.sequence import Aborted
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.test_click import click, refused  # noqa: F401
from tests.host.test_sequence import Rig

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

pytestmark = pytest.mark.slow  # a host

PUBLISH = {"server": "https://files.example.com", "share": "https://share.example.com"}
MODELS = {"claude-code": {"work": "sonnet", "refine": "opus"}, "codex": {"fix": "gpt-5.4"}}
SETTINGS = {"publish": PUBLISH, "model-routing": {"models": MODELS}}
FLAT = {"publish": {"publish": PUBLISH}, "model-routing": {"models": MODELS}}


def preview(h, name):
    return ok(h.c.req("addon.preview", {"name": name}))["data"]


def enable(h, name="model-routing", **over):
    intent = {
        "v": 1,
        "kind": "addon_enable",
        "name": name,
        "settings": copy.deepcopy(FLAT[name]),
        "grant": preview(h, name)["grant"],
    }
    return {**intent, **over}


def on(h, name):
    rows = ok(h.c.req("addon.list"))["data"]["addons"]
    return any(r["name"] == name and r["enabled"] for r in rows)


def settings_of(h):
    """The workspace settings as the host replays them now (a plain dict)."""
    from orch.store.render import thaw

    store = h.served.service.store
    store.refresh()
    return {k: thaw(v) for k, v in store.state.workspace.settings.items()}


def installed(h, name):
    """Is the package of ``name`` in the workspace's store?"""
    from orch.layout import store_of

    return (Path(store_of(h.served.lab.root)) / "addons" / name).exists()


def retamper(prepared):
    """Make a doctored request internally consistent (events, review, digest, fingerprint), as a hostile host would."""
    prepared["material"]["review"] = [review.of(e, prepared["material"]) for e in prepared["events"]]
    digest = review.review_digest(prepared["material"])
    prepared["review_digest"] = digest
    prepared["fingerprint"] = review.fingerprint(digest, prepared["events"], prepared["op"])
    return prepared


# --- addon.preview (read only) --------------------------------------------------------------------


def test_preview_is_the_grant_body_and_writes_nothing(human, served):
    got = preview(human, "publish")
    assert got["needs"] == "publish" and got["on"] is False and got["set_up"] is False
    grant = got["grant"]
    assert set(grant) == {"name", "version", "package_sha256", "capabilities", "fields", "sections", "artifact_kinds"}
    assert preview(human, "publish")["grant"] == grant  # stable
    assert err(human.c.req("addon.preview", {"name": "nope"})) == "not_found"
    assert not installed(human, "publish")  # a built-in is previewed from the shipped bytes in memory
    assert preview(human, "usage")["needs"] is None


# --- the intent (validate) ------------------------------------------------------------------------


def test_validate_takes_exactly_the_closed_shape(human):
    base = enable(human)
    assert sequence.validate_intent(base) == base
    assert [s["op"] for s in sequence.steps(base)] == ["settings.set", "addon.grant"]
    assert sequence.label_of(base) == "turn on model-routing"
    bad = [
        {**base, "extra": 1},
        {k: v for k, v in base.items() if k != "grant"},
        {**base, "v": 2},
        {**base, "name": "usage"},  # an add-on without settings keeps the plain grant
        {**base, "name": "nope"},
        {**base, "settings": {}},
        {**base, "settings": {"publish": PUBLISH}},  # the key of ANOTHER add-on
        {**base, "settings": {"models": MODELS, "publish": PUBLISH}},
        {**base, "settings": {"models": None}},  # clearing is not enabling
        {**base, "settings": {"models": {}}},
        {**base, "settings": {"models": {"claude-code": {}}}},  # no model chosen
        {**base, "settings": {"models": {"claude-code": {"work": "gpt-5"}}}},  # off the closed list
        {**base, "settings": {"models": {"shell": {"work": "opus"}}}},
        {**base, "grant": {**base["grant"], "name": "publish"}},
        {**base, "grant": {**base["grant"], "extra": 1}},
        {**base, "grant": {**base["grant"], "capabilities": ["rm -rf"]}},
        {**base, "grant": {k: v for k, v in base["grant"].items() if k != "version"}},
        {**base, "grant": "model-routing"},
    ]
    for intent in bad:
        with pytest.raises(sequence.SequenceInvalid):
            sequence.validate_intent(intent)
    pub = enable(human, "publish")
    sequence.validate_intent(pub)
    for settings in (
        {"publish": None},
        {"publish": {"server": PUBLISH["server"], "share": PUBLISH["server"]}},  # the same address twice
        {"publish": {"server": "http://files.example.com", "share": PUBLISH["share"]}},  # not https, not loopback
        {"publish": {"server": PUBLISH["server"]}},
        {"publish": {**PUBLISH, "x": 1}},
    ):
        with pytest.raises(sequence.SequenceInvalid):
            sequence.validate_intent({**pub, "settings": settings})


# --- AE1: one intent, both payloads from the intent alone -----------------------------------------


def test_ae1_both_payloads_are_built_from_the_intent_alone(human):
    intent = sequence.validate_intent(enable(human, "publish"))
    assert sequence.expected_payload(intent, 0, "p_x") == {"type": "settings.changed", "set": {"publish": PUBLISH}}
    assert sequence.expected_payload(intent, 1, "p_x") == {"type": "addon.granted", **intent["grant"]}
    assert sequence.step_args(intent, 0, None) == ("settings.set", {"set": {"publish": PUBLISH}})
    assert sequence.step_args(intent, 1, None) == ("addon.grant", {"name": "publish"})
    assert sequence_terminal.labels_for(intent) == (canon.LABELS["sig_ws_event"].encode("ascii"),)


# --- AE2 + the whole run --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["model-routing", "publish"])
def test_the_whole_sequence_lands_settings_first_then_the_grant(human, tmp_path, name):
    rig = Rig(human, tmp_path, enable(human, name))
    out = rig.driver().run(rig.first())
    assert out["landed"] == 2 and out["log"] == "workspace" and out["key"] is None
    types = [e["type"] for _, ev, _ in rig.submitted for e in ev]
    assert types == ["settings.changed", "addon.granted"]
    assert settings_of(human)[needs.NEEDS_SETTINGS[name]] == FLAT[name][needs.NEEDS_SETTINGS[name]]
    assert on(human, name)
    granted = rig.submitted[1][1][0]
    assert {k: v for k, v in granted.items() if k in rig.intent["grant"]} == rig.intent["grant"]
    rec = rig.sessions.get(rig.wid, rig.driver().digest)
    assert [s["state"] for s in rec["steps"]] == ["landed", "landed"] and rec["ticket"] is None


def test_ae2_a_stop_between_the_steps_leaves_the_settings_and_the_add_on_off(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if op == "addon.grant":  # a foreign event on the workspace log between the two steps
            human.run("settings.set", {"set": {"grant_hours": 9}})
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert [e["type"] for _, ev, _ in rig.submitted for e in ev] == ["settings.changed"]
    assert settings_of(human)["models"] == MODELS and not on(human, "model-routing")  # "set up, not on"
    # asking again (a new review) finishes it: the plain grant is allowed now that the settings exist
    human.run("addon.grant", {"name": "model-routing"})
    assert on(human, "model-routing")


def test_ae2_nothing_signed_at_step_zero_changes_nothing(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    rig.port.sign = lambda p: (_ for _ in ()).throw(RuntimeError("no key"))
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert "models" not in settings_of(human) and not on(human, "model-routing")


# --- AE5: the package is bound --------------------------------------------------------------------


def test_ae5_the_host_refuses_step_zero_when_the_package_is_not_the_reviewed_one(human, tmp_path):
    intent = enable(human)
    intent["grant"] = {**intent["grant"], "version": "9.9.9"}  # the package changed since the dashboard read it
    rig = Rig(human, tmp_path, intent)
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer, "sequence": rig.meta0()})
    assert err(res) == "human.request_mismatch" and "package changed" in res["error"]["message"]
    assert "models" not in settings_of(human)


def test_ae5_the_signer_refuses_a_grant_that_is_not_the_reviewed_one(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = copy.deepcopy(real(op, args, meta))
        if op == "addon.grant":
            got["events"][0]["capabilities"] = ["network", "spawn_agent"]  # the host widens what is granted
            retamper(got)
        return got

    rig.port.prepare = prepare
    with pytest.raises(Aborted, match="changed while signing"):
        rig.driver().run(first)
    assert "addon.granted" not in [e["type"] for _, ev, _ in rig.submitted for e in ev]


@pytest.mark.parametrize("k", [0, 1])
def test_the_signer_compares_each_step_byte_for_byte(human, tmp_path, k):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    steps = [first]
    if k == 1:
        rig.driver().run(first)
        steps = [first, rig.prepared[-1]]
    prepared = copy.deepcopy(steps[k])
    sequence.check_step(rig.intent, k, prepared, person=rig.person, first=None, prev_head=None)  # the honest one
    for tamper in (
        lambda p: p["events"][0].update(type="settings.changed" if k else "addon.granted"),
        lambda p: p["events"][0].update(extra=1),
        lambda p: p.update(log="0" * 26),
        lambda p: p["material"].update(ticket={"key": "DEMO-0001", "uid": "0" * 26}),
        lambda p: p["material"].update(texts={"summary": "x"}),
        lambda p: p.update(op="addon.disable"),
        lambda p: p["events"].append(copy.deepcopy(p["events"][0])),
    ):
        bad = copy.deepcopy(prepared)
        tamper(bad)
        with pytest.raises(Aborted):
            sequence.check_step(rig.intent, k, bad, person=rig.person, first=None, prev_head=None)
    if k == 0:
        bad = copy.deepcopy(prepared)
        bad["events"][0]["set"] = {"models": {"claude-code": {"work": "haiku"}}}
        with pytest.raises(Aborted):
            sequence.check_step(rig.intent, 0, bad, person=rig.person, first=None, prev_head=None)
        bad = copy.deepcopy(prepared)
        bad["material"]["before"] = {"repos": {}}  # a before the intent does not set
        with pytest.raises(Aborted):
            sequence.check_step(rig.intent, 0, bad, person=rig.person, first=None, prev_head=None)
    else:
        bad = copy.deepcopy(prepared)
        bad["material"]["before"] = {"models": None}
        with pytest.raises(Aborted):
            sequence.check_step(rig.intent, 1, bad, person=rig.person, first=None, prev_head=None)
