"""The workspace icon, the server side: the typed `icon_set` action (closed lists) and what a host's value becomes."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.adapters.shell import workspace_icon
from orch_dashboard.handoff import PrepareBody, expected, host_call

PAIR = {"icon": "rocket", "colour": "pink"}


def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def test_icon_set_is_one_signed_settings_set_and_null_clears():
    assert host_call(parse({"action": "icon_set", "icon": PAIR})) == ("settings.set", {"set": {"icon": PAIR}})
    assert host_call(parse({"action": "icon_set", "icon": None})) == ("settings.set", {"set": {"icon": None}})


@pytest.mark.parametrize(
    "icon",
    [
        {"icon": "nope", "colour": "pink"},
        {"icon": "rocket", "colour": "#ff0000"},
        {"icon": "rocket"},
        {**PAIR, "url": "http://x/y.png"},
        "rocket",
    ],
)
def test_a_value_outside_the_closed_lists_is_refused_before_the_host_sees_it(icon):
    with pytest.raises(ValidationError):
        parse({"action": "icon_set", "icon": icon})


def test_the_prepared_event_must_be_exactly_that_icon():
    check = expected(parse({"action": "icon_set", "icon": PAIR}))
    assert check([{"type": "settings.changed", "set": {"icon": PAIR}}])
    assert not check([{"type": "settings.changed", "set": {"icon": {"icon": "star", "colour": "pink"}}}])
    assert not check([{"type": "settings.changed", "set": {"icon": PAIR, "grant_hours": 1}}])


def test_only_a_plain_pair_of_the_lists_reaches_the_page():
    assert workspace_icon({"icon": PAIR}) == PAIR
    assert workspace_icon({"icon": None}) is None
    assert workspace_icon({}) is None
    assert workspace_icon({"icon": {"icon": "<img src=x>", "colour": "pink"}}) is None
    assert workspace_icon({"icon": {**PAIR, "extra": 1}}) is None
