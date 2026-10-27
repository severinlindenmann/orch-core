"""Run-9 feedback 6: the admin "Connect a workspace" link (docs/connect-link.md)."""

from __future__ import annotations

import pytest
from webapi_helpers import ORIGIN, Web

from orch_hub.connect import LinkError, build_link, parse_link
from orch_hub.contract.hub import Refused
from orch_hub.serve import FixedInvites

HUB = "0123456789abcdef0123456789abcdef"
VECTOR = f"https://hub.example.test:8443/c#h1.{HUB}.Ab3_x-9Zq.10.0.0.5:8105"


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    web.api.d.invites = FixedInvites([])
    web.api.d.api_origin = ORIGIN
    web.api.d.link_endpoint = lambda: "127.0.0.1:8105"
    yield web
    web.close()


def test_vector_parses():
    assert parse_link(VECTOR) == {
        "origin": "https://hub.example.test:8443",
        "hub_id": HUB,
        "code": "Ab3_x-9Zq",
        "endpoint": "10.0.0.5:8105",
        "owner_user": None,
        "card_url": "https://hub.example.test:8443/api/v1/hub/card",
    }
    assert build_link("https://hub.example.test:8443/", HUB, "Ab3_x-9Zq", "10.0.0.5:8105") == VECTOR
    assert parse_link(f"http://h/c#h1.{HUB}.abc")["endpoint"] is None
    own = "c" * 32
    for ep in (None, "10.0.0.5:8105"):
        got = parse_link(build_link("http://h", HUB, "abc", ep, own))
        assert (got["owner_user"], got["endpoint"]) == (own, ep)


@pytest.mark.parametrize(
    "bad",
    [
        f"https://h/pair#h1.{HUB}.abc",
        "https://h/c#h1.short.abc",
        f"https://h/c#h2.{HUB}.abc",
        f"https://h/c#h1.{HUB}.",
        f"https://h/c#h1.{HUB}.abc.nonsense",
        "ftp://h/c#x",
    ],
)
def test_bad_links_refused(bad):
    with pytest.raises(LinkError):
        parse_link(bad)


def test_admin_mints_a_link_whose_card_matches(w):
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/connect", {}, token=tok)
    assert s == 200, o
    p = parse_link(o["link"])
    assert p["origin"] == ORIGIN and p["endpoint"] == "127.0.0.1:8105" and p["code"] == o["code"]
    s, card, _ = w.call("GET", "/hub/card", origin=None)  # unauthenticated, as the workspace host fetches it
    assert s == 200 and card["card"]["o"]["hub_id"] == p["hub_id"]
    w.api.d.invites.check(o["code"], w.clock())  # valid now


def test_code_is_single_use_and_expires(w):
    inv = w.api.d.invites
    code, _ = inv.mint(1000, 500)
    inv.check(code, 1499)
    with pytest.raises(Refused):
        inv.check(code, 1500)
    code, _ = inv.mint(1000)
    inv.consume(code, "ws")
    with pytest.raises(Refused):
        inv.check(code, 1001)


def test_only_admin_mints(w):
    s, _o, _ = w.call("POST", "/admin/connect", {}, token=w.login(w.ann))
    assert s in (401, 403)


def test_explicit_endpoint(w):
    s, o, _ = w.call("POST", "/admin/connect", {"endpoint": "hub.example.com:8105"}, token=w.login(w.admin))
    assert s == 200 and o["link"].endswith(".hub.example.com:8105~" + w.admin)
    s, o, _ = w.call("POST", "/admin/connect", {"endpoint": "bad endpoint"}, token=w.login(w.admin))
    assert s == 400


def test_minted_link_binds_the_minting_admin_as_owner(w):
    """Blocker 2 (run 12a): the join from the console link records owner_alias = the admin, so Needs-you lists the questions."""
    s, o, _ = w.call("POST", "/admin/connect", {}, token=w.login(w.admin))
    assert s == 200 and o["owner_user"] == w.admin
    assert w.api.d.invites.owner_user(o["code"]) == w.admin
    assert o["link"].endswith("~" + w.admin)  # the owner's join records this alias


def test_minted_link_owner_can_be_picked_but_must_be_a_hub_user(w):
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/connect", {"owner_user": w.ann}, token=tok)
    assert s == 200 and w.api.d.invites.owner_user(o["code"]) == w.ann
    for bad in ("f" * 32, 7):
        s, _o, _ = w.call("POST", "/admin/connect", {"owner_user": bad}, token=tok)
        assert s == 400
