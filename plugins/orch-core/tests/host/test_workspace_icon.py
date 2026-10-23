"""The workspace icon over the host: one closed list in every place, signing words, class, op input and the read."""

from __future__ import annotations

from orch.click.protocol import op_class
from orch.host import signcontent
from orch.ops.person_ops import SET_SCHEMA
from orch.schema import load
from orch.wsicons import COLOUR_LABELS, ICON_LABELS
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PAIR = {"icon": "rocket", "colour": "pink"}


def test_the_closed_lists_are_the_same_in_every_schema():
    for doc in (
        load("event.settings.changed")["properties"]["set"]["properties"]["icon"]["oneOf"][0]["properties"],
        load("workspace")["properties"]["settings"]["properties"]["icon"]["oneOf"][0]["properties"],
        SET_SCHEMA["properties"]["icon"]["oneOf"][0]["properties"],
    ):
        assert doc["icon"]["enum"] == list(ICON_LABELS)
        assert doc["colour"]["enum"] == list(COLOUR_LABELS)


def test_a_plain_click_signs_it_and_the_class_is_click():
    assert op_class("settings.set", args={"set": {"icon": PAIR}}) == "click"
    assert op_class("settings.set", args={"set": {"icon": None}}) == "click"
    assert op_class("settings.set", args={"set": {"icon": PAIR, "models": None}}) == "word"  # the stronger key wins


def test_set_read_and_clear_through_the_host(human, served):
    assert ok(served.client().req("workspace.settings"))["data"]["icon"] is None  # key present: the picker is offered
    _, _, signed = human.run("settings.set", {"set": {"icon": PAIR}})
    assert signed.events[0]["set"] == {"icon": PAIR}
    assert ok(served.client().req("workspace.settings"))["data"]["icon"] == PAIR
    human.run("settings.set", {"set": {"icon": None}})
    assert ok(served.client().req("workspace.settings"))["data"]["icon"] is None


def test_the_op_takes_only_the_closed_lists(human, served):
    for bad in ({"icon": "nope", "colour": "pink"}, {"icon": "rocket", "colour": "#ff0000"}, {"icon": "rocket"},
                {**PAIR, "url": "http://x"}, "rocket"):  # fmt: skip
        assert err(human.prepare_res("settings.set", {"set": {"icon": bad}})) == "invalid.input"


def test_the_review_says_it_in_words(human, served):
    human.run("settings.set", {"set": {"icon": {"icon": "star", "colour": "slate"}}})
    p = human.prepare("settings.set", {"set": {"icon": PAIR}})
    rows = {r["field"]: r for r in signcontent.build(p, verified=True)["fields_changed"]}
    assert rows["Workspace icon"]["after"] == "Rocket, Pink" and rows["Workspace icon"]["before"] == "Star, Grey"
    p = human.prepare("settings.set", {"set": {"icon": None}})
    rows = {r["field"]: r for r in signcontent.build(p, verified=True)["fields_changed"]}
    assert rows["Workspace icon"]["after"] == "(removed: default icon)"


def test_a_value_outside_the_lists_falls_back_to_the_generic_row():
    class Ctx:
        def note(self, _):
            return None

    out: dict = {}
    assert signcontent._icon_rows(out, {"icon": "x", "colour": "pink"}, None, Ctx()) is False
    assert signcontent._icon_rows(out, PAIR, "junk", Ctx()) is False
    assert signcontent._icon_rows(out, PAIR, {"icon": "rocket", "colour": "#fff"}, Ctx()) is False
