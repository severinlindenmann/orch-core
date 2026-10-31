"""#1796: the hub is not served through a proxy: forwarding headers are refused, and only forwarded.py reads them."""

import pathlib
import re

import pytest
from webapi_helpers import Web

from orch_hub import forwarded as FW

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "orch_hub"
HEADERS = ["x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-real-ip", "forwarded"]


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


@pytest.mark.parametrize("name", HEADERS)
def test_every_forwarding_header_is_refused_from_an_untrusted_peer(w, name):
    h = {name: "127.0.0.1", "host": "127.0.0.1"}
    for path in ("/api/v1/health", "/"):
        status, _hd, out = w.api.handle("GET", path, h, b"", "100.71.0.9")
        assert status == 400 and FW.NOT_PROXIED.encode() in out


def test_a_request_without_them_works(w):
    status, _hd, _out = w.api.handle("GET", "/", {"host": "127.0.0.1"}, b"", "100.71.0.9")
    assert status != 400


def test_a_configured_edge_may_forward(w):
    w.api.d.trusted_peers = frozenset({"127.0.0.1"})
    assert FW.refuse(["x-forwarded-for"], "127.0.0.1", w.api.d.trusted_peers) is False
    assert FW.refuse(["x-forwarded-for"], "100.71.0.9", w.api.d.trusted_peers) is True


def test_only_forwarded_py_reads_forwarding_headers():
    pat = re.compile(r"""["'](x-forwarded|x-real-ip)""")
    allowed = {"forwarded.py", "listener.py"}  # listener: the KEEP list of header names it hands to forwarded.py
    for f in SRC.rglob("*.py"):
        if f.name not in allowed:
            assert not pat.search(f.read_text()), f


SUFFIXED = [
    "x-forwarded-prefix",
    "x-forwarded-server",
    "X-Forwarded-Anything",
    "x-client-ip",
    "true-client-ip",
    "cf-connecting-ip",
]


@pytest.mark.parametrize("name", SUFFIXED)
def test_any_x_forwarded_suffix_and_the_client_ip_headers_are_refused_by_the_web_api(w, name):
    status, _hd, out = w.api.handle("GET", "/", {name.lower(): "1", "host": "h"}, b"", "100.71.0.9")
    assert status == 400 and FW.NOT_PROXIED.encode() in out


@pytest.mark.parametrize("name", [*SUFFIXED, "X-Real-IP"])
def test_refuse_rule_applies_to_every_such_header(name):
    assert FW.refuse((name.lower(),), "100.71.0.9", frozenset())
    assert not FW.refuse((name.lower(),), "127.0.0.1", frozenset({"127.0.0.1"}))
