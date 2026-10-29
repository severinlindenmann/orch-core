"""M3 host H-4 T5 (#1575 note, Opus condition 5): unbind and leave revoke every company MCP capability first, then
delete ``org/``. Driven through the real ``hub.leave`` / ``hub.unbind`` ops of a served workspace."""

from __future__ import annotations

import asyncio
import http.client
import json
from types import SimpleNamespace

import pytest

from orch.host import orgmcp, orgstate
from orch.host.launch import launch_argv
from orch.runner.config import parse
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import human, lab, served  # noqa: F401
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.host.test_org_ops import (  # noqa: F401  (fixtures and helpers of the org world)
    dev_workspace,
    no_log_sync_sender,
    org,
    req,
    svc,
    sync,
)
from tests.test_mcp_proxy import FakeUp


def post(port, cap):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(
        "POST", "/m/crm", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        {"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {cap}"},
    )  # fmt: skip
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


@pytest.fixture
def minted(served, org):  # noqa: F811
    """An accepted, served MCP server and a capability for a run of this workspace."""
    org.servers = [org.server("crm")]
    sync(served)
    digest = org.servers[0]["digest"]
    ok(req(served, "org.accept", {"kind": "mcp", "name": "crm", "digest": digest}))
    service = svc(served)
    assert [s["name"] for s in orgstate.served(service)["mcp"]] == ["crm"]
    hub = service.runners
    orgmcp.proxy_for(hub, _upstream=lambda rec, bearer: FakeUp())
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", parse({"v": 1}), workdir=True)
    got = served.h.run(orgmcp.launch_mcp(SimpleNamespace(service=service), hub, "r_1", argv))
    assert got is not None and got["servers"] == ["crm"]
    assert served.h.run(asyncio.to_thread(post, got["port"], got["capability"])) == 200
    yield SimpleNamespace(got=got, hub=hub, service=service, served=served, org=org)
    served.h.run(hub.org_mcp.close()) if getattr(hub, "org_mcp", None) else None


def status(m):
    return m.served.h.run(asyncio.to_thread(post, m.got["port"], m.got["capability"]))


def test_leave_then_unbind_refuses_the_capability_and_deletes_org(minted, human):  # noqa: F811
    m = minted
    human.run("hub.leave", {})
    ok(m.served.client().req("hub.unbind", {}, idem=True))
    assert status(m) == 401
    assert not orgstate.org_dir(m.service).exists()
    assert orgstate.served(m.service) == {"skills": [], "mcp": []}


def test_revoke_happens_before_the_purge(minted, human, monkeypatch):  # noqa: F811
    m = minted
    seen = {}
    real = orgstate.purge

    def spy(service):
        try:
            m.hub.org_mcp._lookup(m.got["capability"])  # noqa: SLF001
            seen["revoked"] = False
        except Exception:
            seen["revoked"] = True
        seen["org_still_there"] = orgstate.org_dir(service).exists()
        real(service)

    monkeypatch.setattr(orgstate, "purge", spy)
    human.run("hub.leave", {})
    ok(m.served.client().req("hub.unbind", {}, idem=True))
    assert seen == {"revoked": True, "org_still_there": True}


def test_a_failed_unbind_revokes_nothing(minted, human):  # noqa: F811
    m = minted
    from tests.host.dispatch_helpers import err

    assert err(m.served.client().req("hub.unbind", {}, idem=True)) == "hub.not_joined"  # not left on the log yet
    assert status(m) == 200 and orgstate.org_dir(m.service).exists()


def test_a_new_binding_serves_nothing_until_a_new_accept(minted, human):  # noqa: F811
    from tests.host.test_hub_join_real import make_card, now_ms

    m = minted
    human.run("hub.leave", {})
    ok(m.served.client().req("hub.unbind", {}, idem=True))
    card = make_card(m.org.hub, now_ms(m.served))
    m.org.op_cert_id = card["op_cert"]["o"]["cert_id"]  # the new card's op cert signs the new bundles
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    args = {"invite": "invite-2", "hub_card": card, "addr": f"127.0.0.1:{m.org.hub.port}"}
    ok(m.served.client().req("hub.connect", args, idem=True))
    assert orgstate.served(m.service) == {"skills": [], "mcp": []}
    assert status(m) == 401  # the old capability is gone for good
    assert sync(m.served)["mcp"] == "new"
    assert orgstate.served(m.service)["mcp"] == []  # offered again, still not accepted


def test_a_connect_to_a_hub_revokes_the_old_capability_through_the_real_op(minted, human):  # noqa: F811
    """Leave, unbind, join and connect again (a rebind): the capability died at the unbind and the connect revokes the
    workspace once more before it writes its state; the new binding serves nothing, and an epoch read before the
    connect cannot mint after it."""
    from tests.host.test_hub_join_real import make_card, now_ms

    m = minted
    proxy = m.hub.org_mcp
    human.run("hub.leave", {})
    ok(m.served.client().req("hub.unbind", {}, idem=True))
    epoch = proxy.epoch(m.service.workspace_id)  # a launch reads this, then reads served...
    card = make_card(m.org.hub, now_ms(m.served))
    m.org.op_cert_id = card["op_cert"]["o"]["cert_id"]
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    args = {"invite": "invite-3", "hub_card": card, "addr": f"127.0.0.1:{m.org.hub.port}"}
    ok(m.served.client().req("hub.connect", args, idem=True))
    assert proxy.epoch(m.service.workspace_id) > epoch  # ...and the connect bumped it
    with pytest.raises(ValueError):
        proxy.mint(m.service.workspace_id, 2, "r_2", [{"name": "crm", "digest": "d"}], epoch=epoch)
