"""V3 A10: the Add-ons page's server half. Each route is a thin proxy to one marketplace host op with closed arguments;
the install hand-off `market_grant` is `addon.grant {name, staged}`: one click like any other signed action (V3-D38;
the click tests are in test_dashboard_click.py), the terminal when click-to-sign is off."""

from types import SimpleNamespace

import pytest
from conftest import ORIGIN
from pydantic import TypeAdapter, ValidationError

from orch_dashboard import gateway as gateway_module
from orch_dashboard import hostlink
from orch_dashboard.errors import HostError
from orch_dashboard.handoff import AddonGrant, MarketGrant, PrepareBody, expected, host_call, plan

STAGE = "st_" + "0123456789abcdef" * 2
IDEM_SHAPE = r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$"


def hdr(token, write=True):
    return {"Authorization": f"Bearer {token}", **({"Origin": ORIGIN} if write else {})}


class Host:
    def __init__(self):
        self.calls, self.answers = [], {}

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        answer = self.answers.get(op, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, path):
    return f"/api/w/{stub.ws}/market{path}"


def refused():
    return HostError({"code": "market.actor_refused", "message": "x", "retryable": False}, 3)


# ---- every route maps to its op ------------------------------------------------------------------------------------
def test_reads_map_to_their_ops(client, token, stub, host):
    assert client.get(url(stub, ""), headers=hdr(token, False)).status_code == 200
    assert client.get(url(stub, "/installed"), headers=hdr(token, False)).status_code == 200
    assert host.calls == [
        ("market.list", {"entries": True}, False, None),
        ("market.installed", {}, False, None),
    ]


WRITES = [
    ("post", "/stage", {"id": "echo"}, "market.stage", {"id": "echo"}),
    (
        "post",
        "/stage",
        {"id": "echo", "url": "https://x.test/m", "update": True},
        "market.stage",
        {"id": "echo", "url": "https://x.test/m", "update": True},
    ),
    ("delete", f"/stage/{STAGE}", None, "market.unstage", {"stage": STAGE}),
    ("post", "/uninstall", {"name": "echo"}, "market.uninstall", {"name": "echo"}),
    ("post", "/refresh", {}, "market.refresh", {}),
    ("post", "/refresh", {"url": "https://x.test/m"}, "market.refresh", {"url": "https://x.test/m"}),
]


@pytest.mark.parametrize("method,path,body,op,args", WRITES)
def test_writes_map_to_their_ops_with_a_fresh_idem(client, token, stub, host, method, path, body, op, args):
    kw = {"json": body} if body is not None else {}
    res = getattr(client, method)(url(stub, path), headers=hdr(token), **kw)
    assert res.status_code == 200, res.text
    ((got_op, got_args, write, idem),) = host.calls
    assert (got_op, got_args, write) == (op, args, True)
    import re

    assert re.fullmatch(IDEM_SHAPE, idem)


@pytest.mark.parametrize("method,path,body,op,args", WRITES)
def test_a_host_refusal_of_an_agent_reaches_the_page_as_403(client, token, stub, host, method, path, body, op, args):
    """The host refuses an agent (core tests prove who); with that refusal faked, only the mapping is proved here."""
    host.answers[op] = refused()
    kw = {"json": body} if body is not None else {}
    res = getattr(client, method)(url(stub, path), headers=hdr(token), **kw)
    assert res.status_code == 403 and res.json()["error"]["code"] == "market.actor_refused"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/stage", {"id": "echo", "extra": 1}),
        ("/stage", {"id": "Echo"}),
        ("/stage", {"id": "../x"}),
        ("/stage", {"id": "echo", "update": "yes"}),
        ("/stage", {"id": "echo", "url": "a b"}),
        ("/stage", {}),
        ("/uninstall", {"name": "echo", "force": True}),
        ("/uninstall", {"name": ""}),
        ("/refresh", {"commit": "a" * 40}),  # a commit pin is human only at the host: not an argument here
        ("/refresh", {"url": "https://x.test/m", "x": 1}),
    ],
)
def test_unknown_keys_and_odd_values_are_refused_before_the_host(client, token, stub, host, path, body):
    res = client.post(url(stub, path), json=body, headers=hdr(token))
    assert res.status_code in (400, 422), res.text
    assert host.calls == []


def test_unstage_takes_only_a_host_stage_id(client, token, stub, host):
    for bad in ("x", "st_zz", STAGE + "0", "ST_" + "0" * 32):
        assert client.delete(url(stub, f"/stage/{bad}"), headers=hdr(token)).status_code in (400, 404, 422)
    assert host.calls == []


def test_routes_need_a_session_and_same_origin_writes(client, token, stub, host):
    assert client.get(url(stub, "")).status_code == 401
    assert client.post(url(stub, "/stage"), json={"id": "echo"}).status_code == 401
    assert client.post(url(stub, "/stage"), json={"id": "echo"}, headers=hdr(token, False)).status_code == 403
    assert host.calls == []


def test_the_market_feature_needs_every_page_op():
    assert set(gateway_module.MARKET_PAGE_OPS) <= set(hostlink.OPS)
    assert "market.add" not in hostlink.OPS  # until "Add marketplace" ships
    assert client_has_no_add_route()


def test_the_market_feature_flag_is_on_only_when_the_host_serves_the_ops(client, token, stub, gateway):
    served = {op: 1 for op in gateway_module.MARKET_PAGE_OPS}
    link = gateway.enabled(stub.ws)
    original = dict(link.welcome["ops"])
    try:
        link.welcome["ops"].update(served)
        assert "market" in gateway.features(stub.ws)
        link.welcome["ops"].pop("market.stage")
        assert "market" not in gateway.features(stub.ws)
    finally:
        link.welcome["ops"].clear()
        link.welcome["ops"].update(original)


# ---- the install hand-off ------------------------------------------------------------------------------------------
def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def test_market_grant_is_addon_grant_with_exactly_name_and_staged():
    body = parse({"action": "market_grant", "name": "echo", "staged": STAGE})
    assert isinstance(body, MarketGrant)
    assert host_call(body) == ("addon.grant", {"name": "echo", "staged": STAGE})
    for bad in (
        {"action": "market_grant", "name": "echo"},
        {"action": "market_grant", "staged": STAGE},
        {"action": "market_grant", "name": "echo", "staged": STAGE, "extra": 1},
        {"action": "market_grant", "name": "echo", "staged": STAGE, "capabilities": []},
        {"action": "market_grant", "name": "Echo", "staged": STAGE},
        {"action": "market_grant", "name": "echo", "staged": "ps_" + "0" * 32},
        {"action": "market_grant", "name": "echo", "staged": STAGE + "\n"},
    ):
        with pytest.raises(ValidationError):
            parse(bad)


def test_the_shipped_addon_grant_still_takes_a_name_only():
    assert isinstance(parse({"action": "addon_grant", "name": "terminals"}), AddonGrant)
    with pytest.raises(ValidationError):
        parse({"action": "addon_grant", "name": "terminals", "staged": STAGE})
    assert parse({"action": "addon_grant", "name": "echo"}).name == "echo"  # an installed add-on switches the same way (run 9)
    with pytest.raises(ValidationError):
        parse({"action": "addon_grant", "name": "dashboard"})


def test_the_prepared_event_must_be_one_grant_of_that_name():
    check = expected(parse({"action": "market_grant", "name": "echo", "staged": STAGE}))
    assert check([{"type": "addon.granted", "name": "echo"}])
    assert not check([{"type": "addon.granted", "name": "other"}])
    assert not check([{"type": "addon.disabled", "name": "echo"}])
    assert not check([{"type": "addon.granted", "name": "echo"}] * 2)


def test_plan_is_one_intent_per_stage_and_skips_the_switch_state_check():
    link = SimpleNamespace(welcome={"peer": {"person": "p_" + "1" * 32}, "ops": {"addon.grant": 1}})
    link.call = lambda *a, **k: pytest.fail(
        "a market grant reads no add-on state: an update is a grant of an active add-on"
    )
    gateway = SimpleNamespace(link=lambda ws: link, call=link.call)
    ws = "a" * 32
    one = plan(gateway, ws, parse({"action": "market_grant", "name": "echo", "staged": STAGE}))
    again = plan(gateway, ws, parse({"action": "market_grant", "name": "echo", "staged": STAGE}))
    other = plan(gateway, ws, parse({"action": "market_grant", "name": "echo", "staged": "st_" + "1" * 32}))
    assert (one.op, one.args) == ("addon.grant", {"name": "echo", "staged": STAGE})
    assert one.idem(ws) == again.idem(ws) != other.idem(ws)


def client_has_no_add_route():
    from orch_dashboard.app import create_app

    return not any("market/add" in getattr(r, "path", "") for r in create_app(1).routes)
