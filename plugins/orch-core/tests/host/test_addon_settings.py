# ruff: noqa: F811
"""G6 B1: the signed add-on settings key, ``settings.set {addons: {<name>: value}}`` (design-g6.md section 3, C5).

Unit part (no host): the general value checks and the model replay. Host part: the granted-schema check inside the
lock, the review (every leaf on its own line), the typed word, and the order install first, settings second."""

from __future__ import annotations

import pytest

from orch.addons import settings_value as sv
from orch.click import protocol
from orch.model import Code
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.market_helpers import add_market, err, files, install, ok
from tests.model.world import World, refused

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "on": {"type": "boolean"},
        "note": {"type": "string", "maxLength": 200},
        "nested": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"tag": {"type": "string", "maxLength": 20}},
        },
    },
}
GOOD = {"mode": "fast", "limit": 5, "on": True, "nested": {"tag": "a"}}


# --------------------------------------------------------------------------------------- the general value checks
@pytest.mark.parametrize(
    "bad",
    [
        {"limit": 1.0},  # F1: jsonschema 2020-12 calls 1.0 an integer; the general check must refuse it first
        {"limit": 2.5},
        {"note": "x" * 201},
        {"note": "a\nb"},
        {"note": "a\x00b"},
        {"note": "a‮b"},
        {"Bad": 1},
        {"a.b": 1},  # B1-1: a dot in a key would let two leaves share one path
        {"a b": 1},
        {"a": {"b": {"c": {"d": {"e": 1}}}}},  # depth 5
        {"note": "x" * 100, "more": ["y" * 190] * 50},  # over 8 KiB
        {"limit": 2**53},
        ["not", "an", "object"],
        "text",
        3,
    ],
)
def test_the_general_checks_refuse(bad):
    assert sv.check_shape(bad) is not None


def test_the_general_checks_accept_plain_values_and_null():
    assert sv.check_shape(None) is None
    assert sv.check_shape({}) is None
    assert sv.check_shape(GOOD) is None
    assert sv.check_shape({"a": {"b": {"c": {"d": 1}}}}) is None  # depth 4
    assert sv.check_shape({"limit": 2**53 - 1}) is None


def test_one_add_on_per_event_with_a_well_formed_name():
    assert sv.check_changes({"demo": GOOD}) is None
    assert sv.check_changes({"demo": None}) is None
    for bad in ({}, {"a": {}, "b": {}}, {"Demo": {}}, {"1demo": {}}, {"de mo": {}}, [], None, {"x" * 41: {}}):
        assert sv.check_changes(bad) is not None


def test_leaves_are_listed_in_key_order_with_their_paths():
    assert sv.leaves({"b": [1, {"c": 2}], "a": {}, "d": {"e": "x"}}) == [
        ("a", {}),
        ("b[0]", 1),
        ("b[1].c", 2),
        ("d.e", "x"),
    ]


# ---------------------------------------------------------------------------------------------- model replay
@pytest.fixture
def w():
    return World().bootstrap({"tom": "member"})


def settings_of(w):
    return {k: v for k, v in w.state().workspace.settings.items() if k == "addons"}


def test_replay_sets_replaces_removes_and_purge_drops(w):
    w.wev("settings.changed", "sev", set={"addons": {"demo": {"mode": "fast"}}})
    w.wev("settings.changed", "sev", set={"addons": {"other": {"on": True}}})
    held = w.state().workspace.settings["addons"]
    assert dict(held["demo"]) == {"mode": "fast"} and dict(held["other"]) == {"on": True}
    w.wev("settings.changed", "sev", set={"addons": {"demo": {"mode": "slow"}}})  # replaces the whole value
    assert dict(w.state().workspace.settings["addons"]["demo"]) == {"mode": "slow"}
    w.wev("settings.changed", "sev", set={"addons": {"other": None}})
    assert list(w.state().workspace.settings["addons"]) == ["demo"]
    w.wev(
        "addon.granted", "sev", name="demo", version="1.0.0", package_sha256="sha256:" + "a" * 64,
        capabilities=[], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip
    w.wev("addon.purged", "sev", name="demo")
    assert "addons" not in w.state().workspace.settings  # purge drops its signed settings
    assert not w.state().workspace.invalid


@pytest.mark.parametrize(
    "bad",
    [
        {"demo": {"limit": 1.0}},
        {"demo": {"x": "y" * 9000}},
        {"demo": {"a": {"b": {"c": {"d": {"e": 1}}}}}},
        {"demo": {"note": "a\nb"}},
        {"demo": {}, "other": {}},
        {"Demo": {}},
        {},
        {"demo": "text"},
    ],
)
def test_replay_refuses_a_malformed_value(w, bad):
    assert refused(w, "workspace", "settings.changed", "sev", set={"addons": bad}) is not None


def test_replay_refuses_add_on_settings_beside_another_key(w):
    code = refused(w, "workspace", "settings.changed", "sev", set={"addons": {"demo": {}}, "grant_hours": 5})
    assert code is not None


def test_only_an_owner_sets_add_on_settings(w):
    assert refused(w, "workspace", "settings.changed", "tom", set={"addons": {"demo": {}}}) == Code.ROLE_DENIED


# ------------------------------------------------------------------------------- the word, the signer, the hub
def test_the_class_is_the_typed_word():
    cls = protocol.op_class
    assert cls("settings.set", args={"set": {"addons": {"demo": {"mode": "fast"}}}}) == "word"
    assert cls("settings.set", args={"set": {"addons": {"demo": None}}}) == "word"
    assert cls("settings.set", events=[{"type": "settings.changed", "set": {"addons": {"demo": {}}}}]) == "word"


# ------------------------------------------------------------------------------------------- host: the schema
PKG = files("cfgdemo", caps=(), api=">=1.1,<2", settings={"title": "Config demo", "schema": SCHEMA})


@pytest.fixture
def installed(served, human, net):
    net.add("cfgdemo", "1.0.0", pkg=PKG, entry_caps=())
    net.add("plain", "1.0.0", caps=(), pkg=files("plain", caps=()))
    add_market(served, net)
    install(served, human, "cfgdemo")
    install(served, human, "plain")
    return served


def settings_set(addons):
    return {"set": {"addons": addons}}


def stored(served):
    from orch.store.render import thaw

    store = served.service.store
    store.refresh()
    return thaw(store.state.workspace.settings.get("addons") or {})


def test_a_valid_value_is_reviewed_signed_and_stored(installed, human):
    prepared = human.prepare("settings.set", settings_set({"cfgdemo": GOOD}))
    assert [e["type"] for e in prepared["events"]] == ["settings.changed"]
    assert prepared["events"][0]["set"] == {"addons": {"cfgdemo": GOOD}}
    human.check(prepared)  # the signer's own check of the request
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    assert stored(installed) == {"cfgdemo": GOOD}


def test_the_review_shows_every_leaf_on_its_own_line(installed, human):
    from orch.host import signcontent

    human.run("settings.set", settings_set({"cfgdemo": {"mode": "slow", "limit": 7}}))
    prepared = human.prepare("settings.set", settings_set({"cfgdemo": GOOD}))
    content = signcontent.build(prepared, verified=True)
    assert content["headline"] == "Settings of cfgdemo (add-on)"
    rows = {r["field"]: r for r in content["fields_changed"]}
    assert set(rows) == {"cfgdemo: limit", "cfgdemo: mode", "cfgdemo: nested.tag", "cfgdemo: on"}
    assert rows["cfgdemo: mode"]["after"] == '"fast"' and rows["cfgdemo: on"]["after"] == "true"
    assert rows["cfgdemo: limit"]["after"] == "5" and rows["cfgdemo: nested.tag"]["after"] == '"a"'
    assert (
        rows["cfgdemo: mode"]["before"] == '"slow"' and rows["cfgdemo: mode"]["before_note"] == "per host, not signed"
    )
    assert rows["cfgdemo: nested.tag"]["before"] is None  # nothing folded: a new leaf has no before


def test_a_removed_leaf_is_shown_as_removed(installed, human):
    from orch.host import signcontent

    human.run("settings.set", settings_set({"cfgdemo": {"mode": "slow", "limit": 7}}))
    content = signcontent.build(
        human.prepare("settings.set", settings_set({"cfgdemo": {"mode": "fast"}})), verified=True
    )
    rows = {r["field"]: r for r in content["fields_changed"]}
    assert rows["cfgdemo: limit"]["after"] == "(removed)" and rows["cfgdemo: limit"]["before"] == "7"


def test_a_newline_in_a_value_cannot_fake_a_second_line():
    from orch.host import signcontent

    out: dict = {}

    class Ctx:
        @staticmethod
        def note(bound):
            return None

    # the general checks refuse a control character; a caller that skipped them still gets one escaped line
    signcontent._row(out, "cfgdemo: note", signcontent._leaf_word("a\nb"))
    assert "\n" not in out["fields_changed"][0]["after"]


def test_a_float_is_refused_before_the_schema_runs(installed, human):  # F1 (binding)
    from jsonschema import Draft202012Validator

    from tests.host.market_helpers import on_worker

    # the premise: the add-on's own schema ACCEPTS 1.0 as an integer, so only the general check can stop it
    assert Draft202012Validator(SCHEMA).is_valid({"limit": 1.0})
    store = installed.service.store

    def check(value):
        with store.locked():
            return sv.check_granted(store, {"cfgdemo": value})

    why = on_worker(installed, lambda: check({"limit": 1.0}))
    assert why == "no decimal numbers"  # not "do not match what that add-on declares": the general check ran first
    assert on_worker(installed, lambda: check({"limit": 1})) is None
    # and the wire refuses a float before the host sees it at all
    with pytest.raises(Exception, match="strict JSON"):
        human.prepare_res("settings.set", settings_set({"cfgdemo": {"limit": 1.0}}))
    assert stored(installed) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"note": "x" * 201},
        {"note": "a\nb"},
        {"a": {"b": {"c": {"d": {"e": 1}}}}},
        {"note": "x" * 100, "extra": ["y" * 190] * 50},
    ],
)
def test_the_host_runs_the_general_checks_first(installed, human, bad):
    assert err(human.prepare_res("settings.set", settings_set({"cfgdemo": bad}))) is not None


@pytest.mark.parametrize(
    "bad",
    [{"mode": "medium"}, {"limit": 0}, {"limit": 101}, {"unknown": 1}, {"on": "yes"}, {"nested": {"tag": "x" * 21}}],
)
def test_a_value_against_the_granted_schema_is_refused(installed, human, bad):
    assert err(human.prepare_res("settings.set", settings_set({"cfgdemo": bad}))) == "invalid.input"


def test_an_add_on_without_settings_or_not_installed_is_refused(installed, human):
    assert err(human.prepare_res("settings.set", settings_set({"plain": {"a": 1}}))) == "invalid.input"
    assert err(human.prepare_res("settings.set", settings_set({"ghost": {"a": 1}}))) == "invalid.input"


def test_a_secret_shaped_string_is_refused(installed, human):
    from tests.host.market_helpers import on_worker

    token = "ghp_" + "a" * 30
    assert err(human.prepare_res("settings.set", settings_set({"cfgdemo": {"note": token}}))) is not None  # the wire
    store = installed.service.store

    def check():
        with store.locked():
            return sv.check_granted(store, {"cfgdemo": {"note": token}})

    assert "access key" in on_worker(installed, check)  # and the settings check itself, were it ever reached


def test_two_add_ons_or_another_key_in_one_event_are_refused(installed, human):
    both = settings_set({"cfgdemo": GOOD, "plain": {}})
    assert err(human.prepare_res("settings.set", both)) is not None
    mixed = {"set": {"addons": {"cfgdemo": GOOD}, "grant_hours": 5}}
    assert err(human.prepare_res("settings.set", mixed)) is not None


def test_null_removes_and_is_signed_like_a_value(installed, human):
    human.run("settings.set", settings_set({"cfgdemo": GOOD}))
    human.run("settings.set", settings_set({"cfgdemo": None}))
    assert stored(installed) == {}


def test_the_schema_is_checked_again_at_submit_inside_the_lock(installed, human):
    """prepared while the package was the granted one, submitted after it was swapped: the second look refuses."""
    from pathlib import Path

    prepared = human.prepare("settings.set", settings_set({"cfgdemo": GOOD}))
    signed = human.sign(prepared)
    manifest = Path(installed.service.store.layout.store) / "addons" / "cfgdemo" / "orch-addon.json"
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original.replace(b'"limit"', b'"limitx"'))
        assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "invalid.input"
    finally:
        manifest.write_bytes(original)
    assert stored(installed) == {}


def test_an_agent_cannot_set_add_on_settings(installed):
    from tests.host.market_helpers import agent

    res = agent(installed, "settings.set", settings_set({"cfgdemo": GOOD}))
    assert err(res) is not None
    assert stored(installed) == {}


def test_a_changed_schema_after_the_grant_cannot_be_used(installed, human):
    """the installed package must be the granted one: a swapped package is refused."""
    from pathlib import Path

    manifest = Path(installed.service.store.layout.store) / "addons" / "cfgdemo" / "orch-addon.json"
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original.replace(b'"limit"', b'"limitx"'))
        assert err(human.prepare_res("settings.set", settings_set({"cfgdemo": {"mode": "fast"}}))) == "invalid.input"
    finally:
        manifest.write_bytes(original)
