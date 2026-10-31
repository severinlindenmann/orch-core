"""#1796: forwarded headers are only a rate key, read in one place; nothing is granted for the VPN or a proxy."""

import pathlib
import re

import pytest

from pubsrv.forwarded import parse_trusted, rate_key
from pubsrv.serve import IP_TABLE_MAX, ShareServer

SRC = pathlib.Path(__file__).resolve().parent.parent / "pubsrv"


VPN = parse_trusted("100.71.0.0/16, fd0f:80b5:c7cd:bbad::/64")


def scope(peer, **h):
    return {
        "client": (peer, 1),
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in h.items()],
    }


def key(peer, nets=VPN, **h):
    sc = scope(peer, **h)
    return rate_key(sc["client"][0], sc["headers"], nets)


def test_spoofed_xff_from_a_non_trusted_peer_is_keyed_on_the_socket():
    assert key("203.0.113.9", x_forwarded_for="1.2.3.4") == "203.0.113.9"
    assert key("100.71.0.5", nets=(), x_forwarded_for="1.2.3.4") == "100.71.0.5"


def test_xff_from_a_trusted_peer_is_keyed_on_the_rightmost_value():
    assert key("100.71.0.5", x_forwarded_for="1.1.1.1, 198.51.100.7") == "198.51.100.7"
    assert key("100.71.0.5", x_real_ip="198.51.100.8") == "198.51.100.8"
    assert key("100.71.0.5", x_forwarded_for="2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"


def test_missing_or_malformed_uses_the_socket():
    assert key("100.71.0.5") == "100.71.0.5"
    assert key("100.71.0.5", x_forwarded_for="not-an-ip") == "100.71.0.5"


def test_forged_loopback_is_only_a_key(tmp_path):
    assert key("100.71.0.5", x_forwarded_for="127.0.0.1", host="127.0.0.1") == "127.0.0.1"
    for f in SRC.glob("*.py"):
        assert not re.search(r"is_private", f.read_text()), f.name


def test_the_table_is_a_bounded_lru(tmp_path):
    srv = ShareServer(tmp_path, trusted_proxy="100.71.0.0/16")
    for i in range(20_000):
        srv._ip_ok(
            scope("100.71.0.5", x_forwarded_for=f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
        )
    assert len(srv._ips) == IP_TABLE_MAX


def test_a_spoofing_peer_cannot_mint_keys(tmp_path):
    srv = ShareServer(tmp_path, trusted_proxy="100.71.0.0/16")
    for i in range(500):
        srv._ip_ok(scope("203.0.113.9", x_forwarded_for=f"10.9.{i // 256}.{i % 256}"))
    assert list(srv._ips) == ["203.0.113.9"]


def test_a_malformed_trusted_list_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ShareServer(tmp_path, trusted_proxy="nope")


def test_the_copy_in_relay_is_identical():
    other = SRC.parent.parent.parent / "relay" / "src" / "orch_relay" / "forwarded.py"
    if other.exists():
        assert other.read_bytes() == (SRC / "forwarded.py").read_bytes()


def test_only_forwarded_py_reads_forwarded_headers():
    pat = re.compile(r"x-forwarded|x-real-ip|\"forwarded\"|proxy_headers\s*=\s*True", re.IGNORECASE)
    for f in SRC.glob("*.py"):
        if f.name != "forwarded.py":
            assert not pat.search(f.read_text()), f.name


def test_a_stalled_body_is_cut_below_the_proxy_idle_limit(monkeypatch):
    import asyncio

    from pubsrv import app as A

    assert A.BODY_IDLE_S <= 90
    monkeypatch.setattr(A, "BODY_IDLE_S", 0.05)

    async def never():
        await asyncio.sleep(10)

    with pytest.raises(ConnectionError):
        asyncio.run(A._read_body(never, 1000))


def test_repeated_and_joined_xff_headers_give_the_same_key_and_a_mapping_is_refused():
    raw2 = [(b"X-Forwarded-For", b"9.9.9.9"), (b"x-forwarded-for", b"198.51.100.7")]
    joined = [(b"x-forwarded-for", b"9.9.9.9, 198.51.100.7")]
    assert (
        rate_key("100.71.0.5", raw2, VPN) == rate_key("100.71.0.5", joined, VPN) == "198.51.100.7"
    )
    with pytest.raises(TypeError):
        rate_key("100.71.0.5", {"x-forwarded-for": "1.2.3.4"}, VPN)


def test_publish_serves_without_a_server_header():
    import inspect

    from pubsrv import cli

    src = inspect.getsource(cli)
    assert (
        src.count("server_header=False") >= 2
    )  # `serve` (uvicorn.run) and the shared _uvicorn_server config
