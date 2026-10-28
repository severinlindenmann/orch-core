"""B2: opt-in display data cannot widen live routing authority."""

from types import SimpleNamespace

import pytest

from orch_hub.contract import vectorgen as G
from orch_hub.contract.hub import Refused
from orch_hub.link import LinkError, Registry
from orch_hub.link.ops import directory_get

PROFILE = {
    "listed": True,
    "tags": ["help"],
    "description": "Support",
    "handles": ["issues"],
    "not_handled": [],
    "accepts": [],
    "contact": "",
    "languages": "",
    "hours": "",
}


@pytest.mark.parametrize("mode", ["full", "selective"])
def test_profile_link_and_sanitizer(hub, mode):
    c = hub.open(first=True)
    c.request("join", hub.join_body(mode=mode))
    assert c.request("profile_put", {**PROFILE, "description": "a\u202eb\u200bc\n\x00"}) == {}
    assert hub.registry.profile(G.WS_A)["description"] == "abc"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**PROFILE, "extra": 1},
        {**PROFILE, "listed": 1},
        *[
            {**PROFILE, key: "x" * (limit + 1)}
            for key, limit in [("description", 300), ("contact", 80), ("languages", 40), ("hours", 40)]
        ],
        *[{**PROFILE, key: ["x"] * 9} for key in ["tags", "handles", "not_handled", "accepts"]],
        *[
            {**PROFILE, key: ["x" * (limit + 1)]}
            for key, limit in [("tags", 24), ("handles", 80), ("not_handled", 80), ("accepts", 24)]
        ],
    ],
)
def test_profile_bounds(hub, body):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    with pytest.raises(LinkError):
        c.request("profile_put", body)
    assert hub.registry.profile(G.WS_A) is None


@pytest.fixture
def directory():
    reg = Registry()
    allowed = {"other", "caller", "unlisted", "selective"}
    access = SimpleNamespace(role_of=lambda user, ws: "edit" if ws in allowed else None)
    for ws in ["caller", "other", "unlisted", "stranger", "selective"]:
        reg.commit_join(
            ws,
            ws,
            "key",
            "pk",
            {"name": ws},
            {"status": "bound", "binding": {"mode": "selective" if ws == "selective" else "full"}},
            owner_alias="alice" if ws == "caller" else "bob",
        )
        reg.put_profile(ws, {**PROFILE, "listed": ws != "unlisted"})
    c = SimpleNamespace(registry=reg, workspace_id="caller", access=access, router=None, body={})
    yield c, allowed
    reg.close()


def test_directory_opt_in_live_authz_and_query(directory):
    c, allowed = directory
    assert [e["workspace_id"] for e in directory_get(c)["entries"]] == ["other"]
    allowed.remove("other")
    assert directory_get(c) == {"entries": []}
    allowed.add("other")
    for query in ["OTHER", "HELP", "SUPPORT", "ISSUES"]:
        c.body = {"query": query}
        assert len(directory_get(c)["entries"]) == 1
    c.body = {"query": "missing"}
    assert directory_get(c) == {"entries": []}
    c.body = {"query": "x" * 81}
    with pytest.raises(Refused):
        directory_get(c)


@pytest.mark.parametrize("action", ["unbound", "revoked", "forget"])
def test_purge(directory, action):
    c, _ = directory
    if action == "forget":
        c.registry.forget("other")
    else:
        c.registry.set_binding_state("other", {"status": action, "binding": None})
    assert c.registry.profile("other") is None
    assert directory_get(c) == {"entries": []}


def test_admin_remove_and_roles(tmp_path):
    from webapi_helpers import WS, Web

    w = Web(tmp_path)
    try:
        token = w.login(w.admin)
        w.reg.ws[WS]["profile"] = PROFILE
        assert w.call("GET", f"/workspaces/{WS}/roles", token=token)[1]["profile"] == PROFILE
        assert "profile" not in w.call("GET", f"/workspaces/{WS}/roles", token=w.login(w.ann))[1]
        w.reg.ws[WS]["status"] = "unbound"
        assert w.call("DELETE", f"/admin/workspaces/{WS}", token=token)[0] == 200
        assert w.reg.profile(WS) is None
    finally:
        w.close()


def test_unbind_link_purges(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    c.request("profile_put", PROFILE)
    c.request("binding_put", {"object": G.unbound(seq=2)})
    assert hub.registry.profile(G.WS_A) is None
    with pytest.raises(LinkError):
        c.request("profile_put", PROFILE)


def test_directory_limit(directory):
    c, allowed = directory
    for i in range(60):
        ws = f"extra{i}"
        c.registry.commit_join(ws, ws, "key", "pk", {"name": ws}, {"status": "bound", "binding": {"mode": "full"}})
        c.registry.put_profile(ws, PROFILE)
        allowed.add(ws)
    assert len(directory_get(c)["entries"]) == 50
