# ruff: noqa: F811
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


# --- AE3: the strongest step decides the confirmation ---------------------------------------------


def test_ae3_publish_is_terminal_only_and_model_routing_is_a_click(human):
    pub = sequence.validate_intent(enable(human, "publish"))
    models = sequence.validate_intent(enable(human, "model-routing"))
    assert sequence.confirm_kind(pub) == "terminal"
    assert sequence.confirm_kind(models) == "click"  # capability-free add-on and a model choice (run 8, 1.6)
    assert not sequence.has_grant(pub) and not sequence.has_grant(models)  # a runner grant only
    # the early check by args, as the click signer asks it before prepare
    assert protocol_class("settings.set", {"set": {"publish": PUBLISH}}) == "terminal"
    assert protocol_class("addon.grant", {"name": "publish"}) == "terminal"  # also alone, not in a sequence


def protocol_class(op, args):
    from orch.click import protocol

    return protocol.op_class(op, args=args)


def test_ae8_existing_kinds_keep_their_digests_steps_and_confirmations():
    from tests.host.test_sequence import intent_of
    from tests.host.test_sequence_quick import quick

    wid, person = "a" * 32, "p_" + "1" * 32
    new = sequence.validate_intent(intent_of())
    q = sequence.validate_intent(quick())
    edit = sequence.validate_intent(
        {
            "v": 1,
            "kind": "edit_ticket",
            "ticket": {"key": "DEMO-0001", "uid": "01J9ZP0000000000000000000A"},
            "ticket_type": "feature",
            "head": "sha256:" + "1" * 64,
            "sections": {"summary": {"was": canon.section_hash("old"), "text": "new"}},
            "people": {"reviewers": {"add": ["p_" + "2" * 32], "remove": []}},
        }
    )
    assert sequence.digest_of(wid, person, new) == PIN_NEW
    assert sequence.digest_of(wid, person, q) == PIN_QUICK
    assert sequence.digest_of(wid, person, edit) == PIN_EDIT
    assert [sequence.confirm_kind(i) for i in (new, q, edit)] == ["click", "click", "word"]
    assert [s["kind"] for s in sequence.steps(new)] == ["new", "set", "section", "section"]
    assert all("log" not in s for i in (new, q, edit) for s in sequence.steps(i))  # only the add-on kind names a log
    assert sequence_terminal.labels_for(new) == (canon.LABELS["sig_ticket_event"].encode("ascii"),)


#: computed on origin/develop 053cec33 (before this feature) and unchanged by it
PIN_NEW = "sha256:8bbb0b9172fd7b52546e005b49900ce7e3ec4e931a93f91779fafc720f0ee838"
#: PIN_QUICK recomputed in #1149: the quick() fixture's check command changed (shell syntax is refused now)
PIN_QUICK = "sha256:3b20a1ceb3beac3bf87ed39259b896c50dd85281bdd5fd021d229f41ef802d61"
PIN_EDIT = "sha256:b5af6594f9bf685844ce2aa42be98ac4cffbc80896cbad35af629547f81534e4"


# --- AE3 on the click path ------------------------------------------------------------------------


def click_prepare(click_, intent):
    intent = sequence.validate_intent(intent)
    meta = {
        "step": 0,
        "of": len(sequence.steps(intent)),
        "digest": sequence.digest_of(click_.served.wid, click_.owner.ref, intent),
        "intent": intent,
    }
    op, args = sequence.step_args(intent, 0, None)
    return click_.prepare(op, args, sequence=meta)


def test_ae3_a_click_never_signs_publish_it_is_refused_at_prepare_and_at_sign(click, served, human):
    intent = enable(human, "publish")
    refused(lambda: click_prepare(click, intent), "human.click_refused", "terminal_only")
    assert click.signatures == 0 and not on(human, "publish")


def test_ae3_publish_is_refused_at_sign_when_the_stored_kind_is_forced_down(click, served, human, monkeypatch):
    """The sign step re-asserts the kind of the stored intent: a prepare that slipped through still signs nothing."""
    intent = enable(human, "model-routing")
    p = click_prepare(click, intent)  # a word sequence: accepted
    v = click.view(p["request_id"])
    click.mono.t += 2
    monkeypatch.setattr(sequence, "confirm_kind", lambda i, person=None: "terminal")
    refused(
        lambda: click.sign(p["request_id"], v["view_digest"], word=v["view"]["confirm"].get("word")),
        "human.click_refused",
        "terminal_only",
    )
    assert click.signatures == 0 and not on(human, "model-routing")


def test_a_sequence_whose_signed_grant_has_capabilities_keeps_the_word(human):
    """The grant step is classed from the intent's signed capability list (run 8, 1.6); no list fails closed."""
    base = enable(human, "model-routing")
    for caps, kind in ((["network"], "word"), ([], "click")):
        intent = sequence.validate_intent({**base, "grant": {**base["grant"], "capabilities": caps}})
        assert sequence.confirm_kind(intent) == kind


def test_model_routing_is_one_plain_click(click, served, human):
    """Run 8 (1.6): a capability-free add-on and a model choice need no typed word."""
    p = click_prepare(click, enable(human, "model-routing"))
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    assert v["view"]["headline"] == "Turn on Model routing"
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 2 and out["duplicate"] is False
    assert on(human, "model-routing") and settings_of(human)["models"] == MODELS
    again = click.sign(p["request_id"], v["view_digest"])  # a repeat answers, never signs again
    assert again["duplicate"] is True and click.signatures == 2


# --- the terminal path: the word, the review, both add-ons ----------------------------------------


def handoff_first(rig):
    op, args = sequence.step_args(rig.intent, 0, None)
    return ok(rig.h.c.req("human.prepare", {"op": op, "args": args, "handoff": True, "sequence": rig.meta0()}))["data"]


@pytest.mark.parametrize("name", ["publish", "model-routing"])
def test_the_terminal_signs_both_add_ons_with_the_add_on_word_and_one_review(human, served, tmp_path, name):
    import contextlib

    from tests.host.test_handoff import Shown
    from tests.host.test_sequence import terminal

    rig = Rig(human, tmp_path, enable(human, name))
    p = handoff_first(rig)
    assert p["request"]["code"] and p["op"] == "settings.set"
    unlocks = []

    class OpenOnce:
        name, person_capable, auth = "test", True, "passphrase"

        def unlocked(self, key_id, *, action, digest, labels, fields=()):
            unlocks.append((action, fields, labels))

            @contextlib.contextmanager
            def cm():
                yield lambda payload: human.person.dk_sign(payload)

            return cm()

    shown = Shown()
    result = terminal(rig, served, shown, p["request"]["code"], backend=OpenOnce())
    assert result["steps"] == 2 and result["key"] is None
    assert shown.words == [f"{name}~{served.wid[-8:]}"]  # the add-on's word, not the SETTINGS word
    assert len(unlocks) == 1 and unlocks[0][2] == (canon.LABELS["sig_ws_event"].encode("ascii"),)
    title = needs.TITLES[name]
    plain = shown.plain[0]
    assert f"Turn on {title}" in plain
    assert "Settings are signed first, then the add-on is turned on" in shown.texts[0] + plain
    assert "2 signatures, ONE confirmation" in shown.texts[0]
    assert on(human, name) and needs.is_set(settings_of(human), name)


def test_the_review_says_the_settings_and_the_grant_in_words(human, tmp_path):
    for name, expect in (
        ("publish", ["Publish: files are sent to", PUBLISH["server"], "Publish: links open at", PUBLISH["share"]]),
        ("model-routing", ["Claude: Do the work", "Sonnet", "Claude: Write requirements and plan", "Opus"]),
    ):
        rig = Rig(human, tmp_path / name, enable(human, name))
        first = rig.first()
        human.check(first)
        from orch.host import signcontent

        content = signcontent.build(first, verified=True)
        assert content["headline"] == f"Turn on {needs.TITLES[name]}"
        flat = json_text(content)
        assert all(x in flat for x in expect), (name, flat[:1500])
        assert "it says it will" in flat or "capabilities" in flat  # the grant is in the same review
        assert any("the settings stay and the add-on stays off" in n for n in content["notes"])
        assert any("2 steps" in n for n in content["notes"])
        from orch.host import signer

        text = signer.render(first, lambda w: ("ws", "ORCH"))
        assert "2 signatures, ONE confirmation" in text
        assert text.count("--- event ") == 2


def json_text(content):
    import json

    return json.dumps(content, sort_keys=True)


def test_ae3_the_word_is_the_add_ons_not_the_settings_word(human, tmp_path):
    from orch.click import view as views
    from orch.host import signer

    rig = Rig(human, tmp_path, enable(human, "model-routing"))
    first = rig.first()
    want = f"model-routing~{human.served.wid[-8:]}"
    assert signer.confirm_word(first) == want
    assert views.word(first, lambda w: ("Acme", "DEMO")) == want
    plain = human.prepare("settings.set", {"set": {"grant_hours": 9}})
    assert signer.confirm_word(plain, lambda w: ("Acme", "DEMO")).endswith(
        "/SETTINGS"
    )  # a plain settings.set: unchanged


# --- AE4: never on without settings (one rule, every path) ----------------------------------------


def test_ae4_addon_grant_is_refused_while_the_setting_is_unset_and_allowed_once_it_is_set(human):
    for name in ("publish", "model-routing"):
        res = human.prepare_res("addon.grant", {"name": name})
        assert err(res) == "invalid.input" and "needs its settings first" in res["error"]["message"]
        assert not installed(human, name)  # refused before anything is installed
    human.run("settings.set", {"set": {"publish": PUBLISH}})  # Publish may be set before it is on
    human.run("addon.grant", {"name": "publish"})
    assert on(human, "publish")
    # an add-on that needs no setting is untouched
    human.run("addon.grant", {"name": "usage"})
    assert on(human, "usage")


def test_ae4_clearing_the_setting_is_refused_while_the_add_on_is_on(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human, "publish"))
    rig.driver().run(rig.first())
    res = human.prepare_res("settings.set", {"set": {"publish": None}})
    assert err(res) == "invalid.input" and "turn it off first" in res["error"]["message"]
    human.run(
        "settings.set", {"set": {"publish": {**PUBLISH, "share": "https://other.example.com"}}}
    )  # changing is fine
    human.run("addon.disable", {"name": "publish"})
    human.run("settings.set", {"set": {"publish": None}})  # off: clearing is allowed
    assert "publish" not in settings_of(human)
    assert err(human.prepare_res("addon.grant", {"name": "publish"})) == "invalid.input"  # and unset again: no grant


def test_ae4_models_with_nothing_chosen_count_as_cleared(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human, "model-routing"))
    rig.driver().run(rig.first())
    for models in (None, {"claude-code": {}}, {"claude-code": {}, "codex": {}}):
        assert err(human.prepare_res("settings.set", {"set": {"models": models}})) == "invalid.input"
    human.run("settings.set", {"set": {"models": {"codex": {"fix": "o3"}}}})  # another choice: fine
    assert needs.is_set({"models": {"claude-code": {}}}, "model-routing") is False
    assert needs.is_set({"models": {"codex": {"fix": "o3"}}}, "model-routing") is True


def test_the_setup_never_plans_these_add_ons(human):
    from orch.setup import review
    from orch.setup.protocol import SetupError

    profile = {
        "name": "x", "prefix": "DEMO", "addons": ["publish"], "method": "terminal", "agent_hours": 8,
        "agent_grant": False, "relay": False,
    }  # fmt: skip
    info = {
        "publish": type("I", (), {"kind": "addon", "needs_runner": False, "needs_terminals": False, "title": "P"})()
    }
    with pytest.raises(SetupError, match="turned on in Settings"):
        review.plan(profile, "/x", info, None, manifest_bytes=lambda n: b"{}")


# --- AE5/AE7: the host's checks at step 0, before anything is signed ------------------------------


def prepare_first(h, rig, **kw):
    op, args = sequence.step_args(rig.intent, 0, None)
    return h.c.req("human.prepare", {"op": op, "args": args, "signer": h.signer, "sequence": rig.meta0()}, **kw)


def test_ae7_a_member_is_refused_before_anything_is_signed_and_nothing_is_installed(served, human, lab):
    """The host's step-0 check: both ops are owner-only, so a member is refused NOW, not after step 0 landed."""
    from orch.host import seqaddon
    from orch.host.errors import HostError

    member = lab.add_member()
    intent = sequence.validate_intent(enable(human, "model-routing"))
    store = served.service.store
    store.refresh()

    def err_of(code, why):
        return HostError(code, why)

    with pytest.raises(HostError) as info:
        seqaddon.host_check_first(store, member.ref, intent, dry_run=False, err=err_of)
    assert info.value.code == "role.denied" and "workspace owners" in info.value.message
    seqaddon.host_check_first(store, human.person.ref, intent, dry_run=False, err=err_of)  # the owner passes
    assert not installed(human, "model-routing") and "models" not in settings_of(human)


def test_the_host_refuses_to_turn_on_what_is_on_already(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human, "publish"))
    rig.driver().run(rig.first())
    again = Rig(human, tmp_path / "again", enable(human, "publish"))
    res = prepare_first(human, again)
    assert err(res) == "invalid.input" and "already on; change its settings instead" in res["error"]["message"]


def test_a_dry_run_installs_and_signs_nothing(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human, "model-routing"))
    res = prepare_first(human, rig, dry_run=True)
    assert res["ok"] and not installed(human, "model-routing") and "models" not in settings_of(human)
    assert not on(human, "model-routing")


def test_install_happens_at_the_grant_step_only_after_the_settings_landed(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human, "model-routing"))
    first = rig.first()
    assert not installed(human, "model-routing")  # step 0 prepared: still nothing on disk
    rig.driver().run(first)
    assert installed(human, "model-routing")
    assert preview(human, "model-routing")["grant"] == rig.intent["grant"]  # the installed bytes are the reviewed ones
    assert preview(human, "model-routing")["on"] is True and preview(human, "model-routing")["set_up"] is True


def test_routing_off_is_lifted_for_the_sequence_only(human, tmp_path):
    models = {"models": MODELS}
    assert err(human.prepare_res("settings.set", {"set": models})) == "invalid.input"  # off: the plain op still refuses
    rig = Rig(human, tmp_path, enable(human))
    ok(prepare_first(human, rig))  # the sequence's first step is allowed
    # a sequence for PUBLISH does not lift it for models, and the plain op stays refused
    assert err(human.prepare_res("settings.set", {"set": models})) == "invalid.input"


def test_the_first_step_of_the_sequence_is_the_intents_and_nothing_else(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    other = {"set": {"models": {"claude-code": {"work": "haiku"}}}}  # not the intent's settings
    res = human.c.req(
        "human.prepare", {"op": "settings.set", "args": other, "signer": human.signer, "sequence": rig.meta0()}
    )
    assert err(res) == "invalid.input"


# --- AE6: the chain -------------------------------------------------------------------------------


def test_ae6_a_foreign_workspace_event_between_the_steps_is_refused_by_the_host(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    real = rig.prepare
    seen = {}

    def prepare(op, args, meta):
        if op == "addon.grant":
            human.run("settings.set", {"set": {"grant_hours": 9}})  # somebody else's event lands on the workspace log
        try:
            return real(op, args, meta)
        finally:
            seen["op"] = op

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert seen["op"] == "addon.grant" and not on(human, "model-routing")


def test_ae6_the_grant_lands_only_on_the_tail_the_signers_settings_left(human, tmp_path):
    """Between prepare and submit of the grant a foreign event lands: the host refuses it under its lock."""
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    rig.hooks[1] = lambda rid, events, idem: human.run("settings.set", {"set": {"grant_hours": 10}})
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert not on(human, "model-routing") and settings_of(human)["models"] == MODELS


def test_a_workspace_step_names_the_log_and_is_only_the_grant(human):
    from orch.host import human as host_human  # noqa: F401

    good = {"step": 1, "of": 2, "digest": "sha256:" + "0" * 64, "prev": "0" * 26, "log": "workspace"}
    args = {"name": "publish"}
    assert sequence.check_meta(good, op="addon.grant", args=args, workspace_id="a" * 32, person="p") == good
    for bad in (
        {**good, "log": "0" * 26},
        {**good, "ticket": "DEMO-0001"},
        {k: v for k, v in good.items() if k != "prev"},
        {**good, "step": 0},
    ):
        with pytest.raises(sequence.SequenceInvalid):
            sequence.check_meta(bad, op="addon.grant", args=args, workspace_id="a" * 32, person="p")
    with pytest.raises(sequence.SequenceInvalid):
        sequence.check_meta(good, op="settings.set", args=args, workspace_id="a" * 32, person="p")  # not another op


# --- partial failure and resume -------------------------------------------------------------------


def test_signed_but_not_confirmed_resumes_with_the_same_bytes(human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()

    class Lost(Exception):
        pass

    def lose(rid, events, idem):
        raise Lost  # the host's answer to the grant is lost after the bytes were signed and recorded

    rig.hooks[1] = lose
    with pytest.raises(Aborted) as info:
        rig.driver().run(first)
    assert info.value.resumable and info.value.landed == 1
    assert settings_of(human)["models"] == MODELS and not on(human, "model-routing")
    rig.hooks.clear()
    again = rig.driver()
    assert again.resumable() is not None
    out = again.run(None)  # a resume: only the recorded bytes are resent
    assert out["landed"] == 2 and on(human, "model-routing")
    types = [e["type"] for _, ev, _ in rig.submitted for e in ev]
    assert (
        types == ["settings.changed", "addon.granted", "addon.granted"] and rig.submitted[1][1] == rig.submitted[2][1]
    )


def test_a_click_refuses_to_start_what_a_live_session_holds(click, served, human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    rig.sessions = click.signer.sessions
    first = rig.first()
    rig.hooks[1] = lambda rid, events, idem: (_ for _ in ()).throw(RuntimeError("lost"))
    with pytest.raises(Aborted):
        rig.driver().run(first)
    p = click_prepare(click, enable(human))
    v = click.view(p["request_id"])
    click.mono.t += 2
    refused(
        lambda: click.sign(p["request_id"], v["view_digest"], word=v["view"]["confirm"].get("word")),
        "human.click_refused",
        "resumable",
    )


# --- review round (Opus LOW-1, LOW-3) --------------------------------------------------------------------------------


def test_low1_two_requests_prepared_apart_cannot_land_on_not_set_up(human, tmp_path):
    """AE4 is re-checked under the submit lock: a grant prepared while the setting was unset ..."""
    # (a) a plain grant prepared when nothing needs checking is impossible (prepare refuses), so prepare the grant
    # of an add-on whose setting is set, then clear the setting before the grant is submitted: the clearing is allowed
    # (the add-on is off), the grant must then be refused at submit.
    human.run("settings.set", {"set": {"publish": PUBLISH}})
    grant = human.prepare("addon.grant", {"name": "publish"})
    human.run("settings.set", {"set": {"publish": None}})
    res = human.submit_res(grant["request"]["id"], human.sign(grant).events)
    assert err(res) == "invalid.input" and "needs its settings first" in res["error"]["message"]
    assert not on(human, "publish")
    # (b) a clearing prepared while the add-on was off, submitted after it was turned on
    human.run("settings.set", {"set": {"publish": PUBLISH}})
    clear = human.prepare("settings.set", {"set": {"publish": None}})
    human.run("addon.grant", {"name": "publish"})
    res = human.submit_res(clear["request"]["id"], human.sign(clear).events)
    assert err(res) == "invalid.input" and "turn it off first" in res["error"]["message"]
    assert on(human, "publish") and settings_of(human)["publish"] == PUBLISH


def test_low3_the_add_on_sequence_never_says_ticket(human, tmp_path):
    assert "ticket" not in sequence.CHANGED_WS
    rig = Rig(human, tmp_path, enable(human))
    first = rig.first()
    rig.driver().run(first)
    human.run("settings.set", {"set": {"grant_hours": 9}})  # a foreign event after the settings step
    stale = {"step": 1, "of": 2, "digest": rig.meta0()["digest"], "prev": "0" * 26, "log": "workspace"}
    res = human.c.req(
        "human.prepare",
        {"op": "addon.grant", "args": {"name": "model-routing"}, "signer": human.signer, "sequence": stale},
    )
    assert err(res) == "human.request_mismatch"
    assert "ticket" not in res["error"]["message"] and "workspace changed" in res["error"]["message"]
    bad = copy.deepcopy(first)
    bad["events"][0]["set"] = {"models": {"claude-code": {"work": "haiku"}}}
    with pytest.raises(Aborted) as info:
        sequence.check_step(rig.intent, 0, bad, person=rig.person, first=None, prev_head=None)
    assert "ticket" not in info.value.reason


def test_low3_the_click_messages_do_not_say_ticket_for_an_add_on(click, served, human, tmp_path):
    rig = Rig(human, tmp_path, enable(human))
    rig.sessions = click.signer.sessions
    first = rig.first()
    rig.hooks[1] = lambda rid, events, idem: (_ for _ in ()).throw(RuntimeError("lost"))
    with pytest.raises(Aborted):
        rig.driver().run(first)
    p = click_prepare(click, enable(human))
    v = click.view(p["request_id"])
    click.mono.t += 2
    e = refused(
        lambda: click.sign(p["request_id"], v["view_digest"], word=v["view"]["confirm"].get("word")),
        "human.click_refused",
        "resumable",
    )
    assert "ticket" not in str(e).lower() and "terminal" in str(e)


# --- review round 2 --------------------------------------------------------------------------------


def test_publish_is_never_turned_on_by_a_click_whatever_the_route(click, served, human):
    from orch.click import protocol

    assert protocol.op_class("addon.grant", args={"name": "publish"}) == "terminal"
    assert protocol.op_class("addon.grant", events=[{"name": "publish"}]) == "terminal"
    assert protocol.op_class("addon.grant", args={"name": "usage"}) == "word"
    assert protocol.op_class("addon.grant", events=[{"name": "usage"}]) == "word"
    # at prepare: the plain grant of Publish is refused before the host is asked
    human.run("settings.set", {"set": {"publish": PUBLISH}})
    refused(lambda: click.prepare("addon.grant", {"name": "publish"}), "human.click_refused", "terminal_only")
    assert click.signatures == 0 and not on(human, "publish")
    # at sign, check_prepared classes the request by its signed events with the same function (asserted above)


def test_the_publish_addresses_are_read_by_a_person_not_by_an_agent_with_a_grant(human, served):
    value = {"server": "https://files.example.com", "share": "https://share.example.com"}
    human.run("settings.set", {"set": {"publish": value}})
    assert ok(human.c.req("workspace.settings"))["data"]["publish"] == value
    agent = ok(human.c.req("workspace.settings", grant=served.grant, session=human_helpers.SESSION))["data"]
    assert "publish" not in agent  # column G
