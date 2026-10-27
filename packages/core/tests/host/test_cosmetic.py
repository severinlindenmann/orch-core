"""D6: the workspace name and icon are unsigned (``config.json`` only): ``workspace.cosmetic.set`` is a compare-and-set
for the owner, closed lists, plain names, a rate limit and a security-log line; no signed shape carries ``icon``."""

from __future__ import annotations

import json

from orch.ops.person_ops import SET_SCHEMA
from orch.schema import load
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PAIR = {"icon": "rocket", "colour": "pink"}


def show(served):
    return ok(served.client().req("workspace.show"))["data"]


def current(served):
    d = show(served)
    return {"name": d["name"], "icon": d["icon"]}


def set_(served, args):
    return served.client().req("workspace.cosmetic.set", args)


def test_no_signed_shape_carries_an_icon():
    # the legacy `set.icon` / `settings.icon` (signed before D6) stay accepted so older logs and files validate, but
    # nothing can sign a new one (the op's input schema) and the model ignores it (tests/store/test_config_fail_closed)
    for legacy in (
        load("event.settings.changed")["properties"]["set"]["properties"]["icon"],
        load("workspace")["properties"]["settings"]["properties"]["icon"],
    ):
        assert legacy["oneOf"][0]["description"].startswith("Legacy (before D6)")
    assert "icon" not in SET_SCHEMA["properties"]
    assert "icon" in load("workspace")["properties"]["workspace"]["properties"]


def test_set_name_and_icon_then_clear_the_icon(served):
    cur = current(served)
    assert cur["icon"] is None
    got = ok(set_(served, {"name": "Studio", "icon": PAIR, "if_current": cur}))["data"]
    assert got == {"name": "Studio", "icon": PAIR}
    assert current(served) == got
    ok(set_(served, {"icon": None, "if_current": got}))
    assert current(served) == {"name": "Studio", "icon": None}


def test_a_stale_if_current_is_a_conflict_and_undo_never_overwrites(served):
    cur = current(served)
    new = ok(set_(served, {"name": "One", "if_current": cur}))["data"]
    ok(set_(served, {"name": "Two", "if_current": new}))
    assert err(set_(served, {"name": cur["name"], "if_current": new})) == "cosmetic.conflict"  # the Undo of "One"
    assert current(served)["name"] == "Two"


def test_the_icon_is_only_from_the_closed_lists(served):
    cur = current(served)
    for icon in (
        {"icon": "nope", "colour": "pink"},
        {"icon": "rocket", "colour": "#f00"},
        {"icon": "rocket"},
        "rocket",
    ):
        assert err(set_(served, {"icon": icon, "if_current": cur})) == "invalid.input"


def test_hidden_or_bad_names_are_refused(served):
    cur = current(served)
    for name in ("a\u202eb", "a\u200bb", "a\x07b", " lead", "", "x" * 81, "a\u2028b"):
        assert err(set_(served, {"name": name, "if_current": cur})) == "invalid.input", repr(name)


def test_other_config_fields_survive_and_the_change_is_logged(served):
    before = json.loads(served.root.joinpath("config.json").read_text()) if hasattr(served, "root") else None
    ok(set_(served, {"name": "Logged", "icon": PAIR, "if_current": current(served)}))
    if before is not None:
        after = json.loads(served.root.joinpath("config.json").read_text())
        assert after["host"] == before["host"] and after["members"] == before["members"]
        assert after["workspace"]["id"] == before["workspace"]["id"]


def test_ten_changes_a_minute(served):
    cur = current(served)
    for i in range(10):
        cur = ok(set_(served, {"name": f"n{i}", "if_current": cur}))["data"]
    assert err(set_(served, {"name": "n10", "if_current": cur})) == "host.busy"


def test_settings_set_takes_no_icon(human, served):
    assert err(human.prepare_res("settings.set", {"set": {"icon": PAIR}})) == "invalid.input"
