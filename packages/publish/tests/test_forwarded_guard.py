"""#1796: forwarded headers are only a rate key, read in one place; nothing is granted for the VPN or a proxy."""

import pathlib
import re

from pubsrv.forwarded import rate_key

SRC = pathlib.Path(__file__).resolve().parent.parent / "pubsrv"


def scope(peer, **h):
    return {"client": (peer, 1), "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in h.items()]}


def test_forwarded_is_the_key():
    assert rate_key(scope("100.71.0.5", x_forwarded_for="198.51.100.7")) == "198.51.100.7"
    assert rate_key(scope("100.71.0.5", x_forwarded_for="1.1.1.1, 198.51.100.7")) == "198.51.100.7"
    assert rate_key(scope("100.71.0.5", x_real_ip="198.51.100.8")) == "198.51.100.8"


def test_ipv6_is_its_64():
    assert rate_key(scope("100.71.0.5", x_forwarded_for="2001:db8:1:2:3:4:5:6")) == "2001:db8:1:2::/64"


def test_missing_or_malformed_uses_the_socket():
    assert rate_key(scope("100.71.0.5")) == "100.71.0.5"
    assert rate_key(scope("100.71.0.5", x_forwarded_for="not-an-ip")) == "100.71.0.5"


def test_forged_loopback_is_only_a_key():
    # a forged 127.0.0.1 is a bucket name, nothing more; no module treats any address as local
    assert rate_key(scope("100.71.0.5", x_forwarded_for="127.0.0.1")) == "127.0.0.1"
    for f in SRC.glob("*.py"):
        assert not re.search(r"is_loopback|is_private|trusted_proxy", f.read_text()), f.name


def test_only_forwarded_py_reads_forwarded_headers():
    pat = re.compile(r"x-forwarded|x-real-ip|\"forwarded\"|proxy_headers\s*=\s*True", re.I)
    for f in SRC.glob("*.py"):
        if f.name != "forwarded.py":
            assert not pat.search(f.read_text()), f.name
