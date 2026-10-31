"""#1796: forwarded headers are only a rate key, read in one place; nothing is granted for the VPN or a proxy."""

import pathlib
import re

from orch_relay.forwarded import rate_key

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "orch_relay"


def test_forwarded_is_the_key():
    assert rate_key("100.71.0.5", {"x-forwarded-for": "9.9.9.9, 198.51.100.7"}) == "198.51.100.7"
    assert rate_key("100.71.0.5", {"x-real-ip": "198.51.100.8"}) == "198.51.100.8"
    assert rate_key("100.71.0.5", {"x-forwarded-for": "2001:db8:1:2:3:4:5:6"}) == "2001:db8:1:2::/64"


def test_missing_or_malformed_uses_the_socket():
    assert rate_key("100.71.0.5", {}) == "100.71.0.5"
    assert rate_key("100.71.0.5", {"x-forwarded-for": "junk"}) == "100.71.0.5"


def test_forged_loopback_host_is_only_a_key():
    h = {"x-forwarded-for": "127.0.0.1", "host": "127.0.0.1"}
    assert rate_key("100.71.0.5", h) == "127.0.0.1"
    for f in SRC.glob("*.py"):
        assert not re.search(r"trusted_proxy|TRUSTED_PROXY", f.read_text()), f.name


def test_only_forwarded_py_reads_forwarded_headers():
    pat = re.compile(r"x-forwarded|x-real-ip|\"forwarded\"|proxy_headers\s*=\s*True", re.I)
    for f in SRC.glob("*.py"):
        if f.name not in ("forwarded.py",):
            assert not pat.search(f.read_text()), f.name
