"""P2-E H2: the card the server setup writes (``https://hub.example.com:443``, settings shape host:port) gives the exact Origin
and Host a browser sends (no default port), and a hub behind the edge accepts it."""

from __future__ import annotations

import pytest
from webapi_helpers import Web

from orch_hub import settings as S
from orch_hub.config import HubConfig

CARD = {
    "v": 1,
    "name": "Hub",
    "company": "",
    "addresses": [{"kind": "private", "web": "https://hub.example.com:443", "link": "100.64.7.9:8105"}],
}


@pytest.mark.parametrize(
    ("web", "origin"),
    [
        ("https://hub.example.com:443", "https://hub.example.com"),
        ("http://hub.example.com:80", "http://hub.example.com"),
        ("https://hub.example.com:8443", "https://hub.example.com:8443"),
        ("http://127.0.0.1:8100", "http://127.0.0.1:8100"),
        ("https://[fd00::1]:443", "https://[fd00::1]"),
    ],
)
def test_origin_of_drops_only_the_default_port(web, origin):
    assert S.origin_of(web) == origin


def test_serve_origins_are_what_the_browser_sends_and_pass_the_edge_config(tmp_path):
    S.save(tmp_path, S.validate(CARD))
    origins, api = S.serve_origins(tmp_path)
    assert origins == frozenset({"https://hub.example.com"}) and api == "https://hub.example.com"
    cfg = HubConfig(  # behind_edge needs https-only origins and a trusted peer: --behind-edge --trusted-peer 127.0.0.1
        dev=False,
        listen="127.0.0.1:0",
        web_listen="127.0.0.1:8100",
        behind_edge=True,
        trusted_peers=frozenset({"127.0.0.1"}),
        allowed_origins=origins,
        api_origin=api,
    )
    assert cfg.resolved_api_origin() == "https://hub.example.com"


def test_a_web_api_built_from_those_origins_accepts_the_https_origin_and_host(tmp_path):
    (tmp_path / "s").mkdir()
    S.save(tmp_path / "s", S.validate(CARD))
    origins, _ = S.serve_origins(tmp_path / "s")
    w = Web(tmp_path)
    try:
        for o in origins:
            w.api.allow_origin(o)
        status, body, _ = w.call(
            "POST",
            "/session",
            {
                "user_id": w.admin,
                "password": "correct horse battery",
                "totp": w.env.next_code(w.admin),
                "client": "browser",
            },
            origin="https://hub.example.com",
            host="hub.example.com",
        )
        assert status == 200, body
        # the same request with the port the card carries is NOT what a browser sends: refused
        bad = w.call(
            "POST",
            "/session",
            {"user_id": w.admin, "password": "x", "totp": "000000", "client": "browser"},
            origin="https://hub.example.com:443",
            host="hub.example.com:443",
        )
        assert bad[0] in (400, 403)
    finally:
        w.close()
