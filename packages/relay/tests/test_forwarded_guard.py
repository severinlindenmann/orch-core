"""#1796: the forwarded address is only a rate key, honoured from a trusted-proxy list, read in one place."""

import pathlib
import re

import pytest

from orch_relay.forwarded import parse_trusted
from orch_relay.forwarded import rate_key as _real

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "orch_relay"


def rate_key(peer, headers, nets=()):
    """Test helper: a dict of lower-case names as the raw ASGI header list."""
    return _real(peer, [(k.encode(), v.encode()) for k, v in headers.items()], nets)


VPN = parse_trusted("100.71.0.0/16, fd0f:80b5:c7cd:bbad::/64")


def test_spoofed_xff_from_a_non_trusted_peer_is_keyed_on_the_socket():
    assert rate_key("203.0.113.9", {"x-forwarded-for": "1.2.3.4"}, VPN) == "203.0.113.9"
    assert rate_key("203.0.113.9", {"x-real-ip": "1.2.3.4"}, VPN) == "203.0.113.9"
    assert (
        rate_key("100.71.0.5", {"x-forwarded-for": "1.2.3.4"}) == "100.71.0.5"
    )  # empty list trusts nothing


def test_xff_from_a_trusted_peer_is_keyed_on_the_rightmost_value():
    assert (
        rate_key("100.71.0.5", {"x-forwarded-for": "9.9.9.9, 198.51.100.7"}, VPN) == "198.51.100.7"
    )
    assert rate_key("100.71.0.5", {"x-real-ip": "198.51.100.8"}, VPN) == "198.51.100.8"
    assert (
        rate_key("fd0f:80b5:c7cd:bbad::7", {"x-forwarded-for": "2001:db8:1:2:3:4:5:6"}, VPN)
        == "2001:db8:1:2::/64"
    )


def test_missing_or_malformed_uses_the_socket():
    assert rate_key("100.71.0.5", {}, VPN) == "100.71.0.5"
    assert rate_key("100.71.0.5", {"x-forwarded-for": "junk"}, VPN) == "100.71.0.5"


def test_forged_loopback_host_is_only_a_key():
    h = {"x-forwarded-for": "127.0.0.1", "host": "127.0.0.1"}
    assert rate_key("100.71.0.5", h, VPN) == "127.0.0.1"
    for f in SRC.rglob("*.py"):
        assert not re.search(r"is_loopback|is_private|\.is_global", f.read_text()), f.name


def test_a_malformed_trusted_list_is_refused():
    with pytest.raises(ValueError):
        parse_trusted("not-a-network")


def test_only_forwarded_py_reads_forwarded_headers():
    pat = re.compile(r"""["']x-(forwarded|real-ip)|proxy_headers\s*=\s*True""", re.IGNORECASE)
    for f in SRC.rglob("*.py"):
        if f.name not in (
            "forwarded.py",
            "common.py",
        ):  # common.py: the one-time "set the trusted list" warning
            assert not pat.search(f.read_text()), f.name


def test_the_copy_in_publish_is_identical():
    other = SRC.parent.parent.parent / "publish" / "pubsrv" / "forwarded.py"
    if other.exists():  # a monorepo checkout
        assert other.read_bytes() == (SRC / "forwarded.py").read_bytes()


def test_repeated_and_joined_xff_headers_give_the_same_key_and_a_mapping_is_refused():
    raw2 = [(b"X-Forwarded-For", b"9.9.9.9"), (b"x-forwarded-for", b"198.51.100.7")]
    joined = [(b"x-forwarded-for", b"9.9.9.9, 198.51.100.7")]
    assert _real("100.71.0.5", raw2, VPN) == _real("100.71.0.5", joined, VPN) == "198.51.100.7"
    # X-Real-IP only when there is no X-Forwarded-For
    assert _real("100.71.0.5", [(b"x-real-ip", b"198.51.100.8"), *joined], VPN) == "198.51.100.7"
    with pytest.raises(TypeError):
        _real("100.71.0.5", {"x-forwarded-for": "1.2.3.4"}, VPN)
    # the publish copy answers the same (identical file, checked above)
    try:
        from pubsrv.forwarded import rate_key as pub
    except ImportError:  # relay-only environment
        return
    assert pub("100.71.0.5", raw2, VPN) == "198.51.100.7"


def test_an_empty_xff_or_a_zone_id_uses_the_socket_not_x_real_ip():
    assert (
        rate_key("100.71.0.5", {"x-forwarded-for": "", "x-real-ip": "198.51.100.9"}, VPN)
        == "100.71.0.5"
    )
    assert (
        rate_key("100.71.0.5", {"x-forwarded-for": "  ", "x-real-ip": "198.51.100.9"}, VPN)
        == "100.71.0.5"
    )
    assert rate_key("100.71.0.5", {"x-forwarded-for": "fe80::1%eth0"}, VPN) == "100.71.0.5"
    assert rate_key("100.71.0.5", {"x-real-ip": "fe80::1%eth0"}, VPN) == "100.71.0.5"
