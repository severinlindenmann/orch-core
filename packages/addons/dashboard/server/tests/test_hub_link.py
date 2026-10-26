"""Run-9 feedback 6: the dashboard reads a connect link through the host and passes the hub address to hub.connect."""

import json

from test_dashboard_hub import HUB32, WS, Link, client, connect, connect_client

LINK = f"http://10.0.0.5:8106/c#h1.{HUB32}.Ab3_x-9Zq.10.0.0.5:8105"
GOT = {"hub_id": HUB32, "hub_card": {"o": {"hub_id": HUB32}, "sig": "x"}, "code": "Ab3_x-9Zq",
       "endpoint": "10.0.0.5:8105", "name": "10.0.0.5:8106", "dev": True}  # fmt: skip


def test_the_link_is_read_by_the_host_and_the_page_gets_the_hub_and_the_pieces_of_the_join():
    link = Link({"hub.link": GOT})
    got = client(link).post(f"/api/w/{WS}/hub/link", json={"link": LINK})
    assert got.status_code == 200 and link.calls == [("hub.link", {"link": LINK})]
    body = got.json()
    assert body["hub_id"] == HUB32 and body["name"] == "10.0.0.5:8106" and body["code"] == "Ab3_x-9Zq"
    assert body["endpoint"] == "10.0.0.5:8105" and body["dev"] is True
    assert json.loads(body["hub_card"]) == GOT["hub_card"]


def test_a_link_with_hidden_characters_or_outer_spaces_never_reaches_the_host():
    link = Link({"hub.link": GOT})
    c = client(link)
    for bad in (" " + LINK, LINK + "‮", ""):
        assert c.post(f"/api/w/{WS}/hub/link", json={"link": bad}).status_code in (400, 422)
    assert link.calls == []


def test_connect_passes_the_address_of_the_link_to_the_host():
    link = Link({"hub.connect": {"mode": "selective"}})
    assert connect(connect_client(link), addr="10.0.0.5:8105").status_code == 200
    assert link.calls[0][1]["addr"] == "10.0.0.5:8105"
    assert connect(connect_client(link), addr="x y").status_code in (400, 422)


def test_hub_link_is_a_read_op_of_the_host_link():
    # without it HostLink.call("hub.link") treated the op as a write and demanded an idem: the route answered 500
    from orch_dashboard import hostlink

    assert "hub.link" in hostlink.OPS
