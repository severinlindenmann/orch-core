"""R2 T3 (#1590, D93): when the host refuses a hub switch for a workspace with remote access (`switch.refused`), the
dashboard answers a plain 403 and opens no session; the ticket was never sent to the hub, so it is still unused."""

# ruff: noqa: F811 - pytest fixtures imported by name and used as arguments

from conftest import ORIGIN
from test_dashboard_switch import TICKET, auth, client, link  # noqa: F401 - the switch fixtures

from orch_dashboard.errors import fail

H = {"Origin": ORIGIN}


def test_a_refused_switch_is_a_plain_403_and_opens_nothing(client, link, auth):
    link.answers["redeem"] = fail("switch.refused", "this workspace cannot be opened from another computer")
    res = client.post("/auth/switch", json={"t": TICKET}, headers=H)
    assert res.status_code == 403
    err = res.json()["error"]
    assert err["code"] == "switch.refused" and err["message"] == "Open this workspace on its own computer."
    assert "token" not in res.json() and not auth.sessions


def test_the_refusal_is_not_a_bad_ticket(client, link):
    """A hub refusal is the one 401 for every bad ticket; the host's D93 refusal is its own answer, and it costs the
    source no extra rate charge (the ticket was good and never sent)."""
    link.answers["redeem"] = fail("hub.refused", "bad_ticket")
    assert client.post("/auth/switch", json={"t": TICKET}, headers=H).status_code == 401
    link.answers["redeem"] = fail("switch.refused")
    assert client.post("/auth/switch", json={"t": TICKET}, headers=H).status_code == 403
