"""Design-system widget API 2.1 additions (spec §3.2, all additive) and the E1 rule: Action.quiet, Table.key,
Tabs, Time, non-empty titles."""
import pytest

from addon_fixtures import GOOD
from orch.addons.manifest import parse_manifest
from orch.addons.widgets import (KV, Action, Badge, Callout, Card, Link, Table, Tabs, Text, Tile, Time,
                                 widget_problems)

M = parse_manifest({**GOOD, "actions": [{"id": "rerun", "label": "Rerun failed"}]})


def problems(w, slot="page.hello-status"):
    return widget_problems(w, slot=slot, manifest=M)


def test_new_fields_default_to_the_old_behaviour():
    assert Action("rerun", "Rerun failed").quiet is False
    assert Table(("A",), ()).key == 0


def test_api_2_1_widgets_pass():
    assert problems(Action("rerun", "Rerun failed", quiet=True)) == []
    assert problems(Table(("Repo", "Key"), (("a", "b"),), key=1)) == []
    assert problems(Tabs((Link("Files", "/addons/x/", current=True), Link("Messages", "/addons/x/?v=m")), label="View")) == []
    assert problems(Time("2026-10-03T14:32:00Z")) == []
    assert problems(Time("2026-10-03T14:32:00+02:00", style="at")) == []
    assert problems(Table(("Run", "When"), (("r1", Time("2026-10-03T14:32:00Z")),))) == []
    assert problems(KV((("Updated", Time("2026-10-03T14:32:00Z")),))) == []


@pytest.mark.parametrize("w, needle", [
    (Action("rerun", "Rerun", quiet="yes"), "quiet"),
    (Table(("A", "B"), (), key=2), "key"),
    (Table(("A", "B"), (), key=-1), "key"),
    (Table(("A",), (), key=True), "key"),
    (Tabs((Text("x"),)), "Link"),
    (Tabs(None), "must be a tuple"),
    (Tabs((Link("a", "/x"),), label=3), "label"),
    (Time("yesterday"), "ISO"),
    (Time(5), "string"),
    (Time("2026-10-03T14:32:00Z", style="soon"), "style"),
    (Badge("ok", ""), "empty"),
    (Badge("ok", "   "), "empty"),
    (Callout("warn", ""), "empty"),
    (Card(" "), "empty"),
])
def test_bad_api_2_1_widgets_are_reported(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_tabs_only_on_the_addon_page():
    tabs = Tabs((Link("Files", "/addons/x/"),))
    assert any("Tabs is only allowed" in p for p in problems(tabs, slot="ticket.code"))
    assert problems(tabs, slot="page.hello-status") == []


def test_empty_tile_label_is_an_error():
    assert any("empty" in p for p in problems(Tile("", 1, sub="x"), slot="today.summary"))
