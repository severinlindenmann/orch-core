"""D6, the server side: the cosmetic route is a thin proxy to `workspace.cosmetic.set` (no signature), and only a plain
pair of the closed lists reaches the page."""

import pytest
from test_market_routes import Host, hdr

from orch_dashboard.adapters.shell import workspace_icon

PAIR = {"icon": "rocket", "colour": "pink"}
CUR = {"name": "Studio", "icon": None}


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def test_the_route_sends_the_change_and_the_compare_value(client, token, stub, host):
    r = client.put(
        f"/api/w/{stub.ws}/workspace/cosmetic",
        json={"name": "Lab", "icon": PAIR, "if_current": CUR},
        headers=hdr(token),
    )
    assert r.status_code == 200
    ((op, args, write, idem),) = host.calls
    assert (op, write) == ("workspace.cosmetic.set", True) and idem.startswith("a_")
    assert args == {"name": "Lab", "icon": PAIR, "if_current": CUR}


def test_null_icon_clears_and_an_omitted_icon_is_left_alone(client, token, stub, host):
    client.put(f"/api/w/{stub.ws}/workspace/cosmetic", json={"icon": None, "if_current": CUR}, headers=hdr(token))
    client.put(f"/api/w/{stub.ws}/workspace/cosmetic", json={"name": "X", "if_current": CUR}, headers=hdr(token))
    assert host.calls[0][1] == {"icon": None, "if_current": CUR}
    assert host.calls[1][1] == {"name": "X", "if_current": CUR}


def test_extra_fields_and_a_missing_compare_value_are_refused(client, token, stub, host):
    url = f"/api/w/{stub.ws}/workspace/cosmetic"
    assert client.put(url, json={"name": "X"}, headers=hdr(token)).status_code == 400
    assert client.put(url, json={"name": "X", "if_current": CUR, "x": 1}, headers=hdr(token)).status_code == 400
    assert host.calls == []


def test_only_a_plain_pair_of_the_lists_reaches_the_page():
    assert workspace_icon({"icon": PAIR}) == PAIR
    assert workspace_icon({"icon": None}) is None
    assert workspace_icon({}) is None
    assert workspace_icon({"icon": {"icon": "<img src=x>", "colour": "pink"}}) is None
    assert workspace_icon({"icon": {**PAIR, "extra": 1}}) is None
