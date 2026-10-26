"""The shared origin vectors (same file in hub, publish, relay): stored-origin and card relay_url rule."""

import json
import pathlib

import pytest

from orch.wsproto import card, wire

V = json.loads((pathlib.Path(__file__).resolve().parents[4] / "contracts" / "origin_http.json").read_text())


@pytest.mark.parametrize("o", V["accept"])
def test_accept(o):
    assert wire.check_origin(o) == o
    assert card._RELAY_URL.fullmatch(o)


@pytest.mark.parametrize("o", V["refuse"])
def test_refuse(o):
    with pytest.raises(wire.WireError):
        wire.check_origin(o)
    assert not card._RELAY_URL.fullmatch(o)
