"""Opus R6 review M1: a host whose last label starts with a digit is a whole canonical dotted quad or malformed."""

from __future__ import annotations

import pytest

from orch.hubclient.linkaddr import parse_link_address


@pytest.mark.parametrize(
    "addr",
    ["wss://169.254.169.0xfe", "ws://127.0.0.0x1:8080", "0x7f.0.0.0x1:7443", "ws://0x7f000001", "wss://10.0.0.0x5",
     "ws://0.0.0.0x0:80"],
)  # fmt: skip
def test_numeric_looking_names_are_malformed(addr) -> None:
    with pytest.raises(Exception, match="malformed"):
        parse_link_address(addr)


def test_real_names_and_quads_still_parse() -> None:
    assert parse_link_address("wss://hub.example.com").host == "hub.example.com"
    assert parse_link_address("10.0.0.5:7443").host == "10.0.0.5"
    assert parse_link_address("wss://a.b1c").host == "a.b1c"
