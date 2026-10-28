"""FB15: "Approve in the hub instead" for an add-on. ONE narrow route prepares the add-on grant as an unbound request
for the hub (`human.prepare {handoff: true}`, no signer): the owner, from a full session, of a workspace joined to a
hub in full mode, for an add-on grant only. The generic hand-off routes stay gone."""

import time

import pytest
from test_market_routes import hdr

from orch_dashboard.adapters.handoff import fingerprint, review_digest
from orch_dashboard.policy import LOOPBACK
from orch_dashboard.routegate import REFUSED, TABLE

OWNER = "p_" + "1" * 32
MEMBER = "p_" + "2" * 32
RID = "hr_01J9ZP0000000000000000HND1"
SHA = "sha256:" + "a" * 64


def joined(mode="full", is_joined=True):
    return {"signing": mode, "joined": is_joined}


class Link:
    """A host link: the answers by op; `human.prepare` answers the consistent prepared add-on grant."""

    def __init__(self, ws, person=OWNER, role="owner", hub=None):
        self.ws, self.calls = ws, []
        self.welcome = {"peer": {"person": person}, "ops": {"addon.grant": 1}}
        self.answers = {
            "member.list": {"members": [{"person": person, "role": role}]},
            "hub.status": hub if hub is not None else joined(),
            "addon.list": {"addons": []},
            "activity.list": {"items": []},
            "hub.handoff": {
                "mode": "full", "where": "hub", "kind": "addon", "pending": True,
                "summary": {"kind": "addon", "name": "wiki", "version": "1.0.0", "package_sha256": SHA,
                            "capabilities": ["network"]},
                "short_hash": "aaaa aaaa aaaa",
                "url": f"https://hub.example.test:4443/#/w/{ws}/addon/eyJhIjoxfQ",
            },
            "hub.switch": RuntimeError("no rail"),
            "human.pending": {"requests": [{"id": RID, "op": "addon.grant", "key": None, "state": "prepared",
                                            "expires_at": "2099-01-01T00:00:00Z", "event_ids": [], "seq": None}]},
        }  # fmt: skip

    def supports(self, op, cap="ops/1"):
        return True

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        if op == "human.prepare":
            return prepared(self.ws, args["args"]["name"])
        got = self.answers[op]
        if isinstance(got, Exception):
            raise got
        return got

    def close(self):
        pass


def prepared(ws, name):
    payload = {"name": name, "version": "1.0.0", "package_sha256": SHA, "capabilities": ["network"]}
    actor = {"kind": "person", "id": OWNER, "device": None}
    envelope = {"v": 2, "id": "01J9ZP00000000000000000EV1", "type": "addon.granted", "actor": actor, "auth": None}
    event = {**envelope, "hash_v": 1, "roster_v": 1, "based_on": None, **payload}
    material = {
        "kind": "payload",
        "ticket": None,
        "review": [{"event_id": event["id"], "type": "addon.granted", "payload": payload}],
    }
    digest = review_digest(material)
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    return {
        "request": {"id": RID, "state": "prepared", "expires_at": expires},
        "op": "addon.grant",
        "workspace_id": ws,
        "log": "workspace",
        "events": [event],
        "material": material,
        "review_digest": digest,
        "fingerprint": fingerprint(digest, [event], "addon.grant"),
        "origin": {"uid": 0, "pid": 0, "client": "test", "created_at": "2026-01-01T00:00:00Z"},
    }


@pytest.fixture
def use(gateway, monkeypatch):
    def put(link):
        monkeypatch.setattr(gateway, "enabled", lambda ws: link)
        monkeypatch.setattr(gateway, "link", lambda ws: link)  # the session's person (`person_of`) is the link's here
        return link

    return put


def path(stub, rest="prepare"):
    return f"/api/w/{stub.ws}/hub/approve/{rest}"


def prepares(link):
    return [c for c in link.calls if c[0] == "human.prepare"]


def test_the_owner_prepares_an_add_on_grant_for_the_hub_and_gets_the_one_link(client, token, stub, use):
    link = use(Link(stub.ws))
    r = client.post(path(stub), json={"action": "addon_grant", "name": "wiki"}, headers=hdr(token))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["id"] == RID and got["state"] == "prepared" and got["kind"] == "addon"
    assert got["short_hash"] == "aaaa aaaa aaaa" and got["url"].endswith("/addon/eyJhIjoxfQ")
    assert got["summary"] == {"kind": "addon", "name": "wiki", "version": "1.0.0", "capabilities": ["network"]}
    ((_, args, write, idem),) = prepares(link)
    # unbound, for the hub: handoff true and no signer; core sets the hub_handoff mark, never this server
    assert args == {"op": "addon.grant", "args": {"name": "wiki"}, "handoff": True} and write and idem
    assert ("hub.handoff", {"kind": "addon", "request": RID}, False, None) in link.calls
    # the poll answers for the request this route prepared
    polled = client.get(path(stub, RID), headers=hdr(token, write=False))
    assert polled.status_code == 200 and polled.json()["state"] == "prepared"


def test_a_marketplace_grant_names_its_stage(client, token, stub, use):
    link = use(Link(stub.ws))
    body = {"action": "market_grant", "name": "wiki", "staged": "st_" + "b" * 32}
    assert client.post(path(stub), json=body, headers=hdr(token)).status_code == 200
    ((_, args, _, _),) = prepares(link)
    assert args["args"] == {"name": "wiki", "staged": "st_" + "b" * 32} and args["handoff"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"action": "addon_disable", "name": "wiki"},
        {"action": "settings_set", "grant_hours": 24},
        {"action": "comment", "key": "DEMO-0001", "text": "x"},
        {"action": "addon_grant", "name": "wiki", "signer": {"device": "d_" + "0" * 32, "auth": "passphrase"}},
        {"action": "addon_grant", "name": "wiki", "handoff": False},
        {"action": "addon_grant"},
    ],
)
def test_any_other_action_is_refused_before_the_host_is_asked(client, token, stub, use, body):
    link = use(Link(stub.ws))
    r = client.post(path(stub), json=body, headers=hdr(token))
    assert r.status_code in (400, 422)
    assert prepares(link) == []


def test_a_member_is_refused(client, token, stub, use):
    link = use(Link(stub.ws, person=MEMBER, role="member"))
    r = client.post(path(stub), json={"action": "addon_grant", "name": "wiki"}, headers=hdr(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert prepares(link) == []


def test_the_sessions_person_must_be_the_owner_not_the_links_peer(client, token, stub, use, monkeypatch):
    link = use(Link(stub.ws))
    members = [{"person": OWNER, "role": "owner"}, {"person": MEMBER, "role": "member"}]
    link.answers["member.list"] = {"members": members}
    monkeypatch.setattr(client.app.state, "person_of", lambda ws: MEMBER)
    r = client.post(path(stub), json={"action": "addon_grant", "name": "wiki"}, headers=hdr(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert prepares(link) == []


def test_a_switch_session_is_read_only(client, auth, stub, use):
    link = use(Link(stub.ws))
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    r = client.post(path(stub), json={"action": "addon_grant", "name": "wiki"}, headers=hdr(switch))
    assert r.status_code == 403 and r.json()["error"]["code"] == "session.readonly"
    assert link.calls == []


@pytest.mark.parametrize("hub", [joined("standalone", False), joined("selective"), joined("unknown"), {}])
def test_a_workspace_not_joined_in_full_mode_is_refused(client, token, stub, use, hub):
    link = use(Link(stub.ws, hub=hub))
    r = client.post(path(stub), json={"action": "addon_grant", "name": "wiki"}, headers=hdr(token))
    assert r.status_code >= 400 and r.json()["error"]["code"] == "hub.not_joined"
    assert prepares(link) == []


def test_the_poll_answers_only_for_a_request_this_route_prepared(client, token, stub, use):
    link = use(Link(stub.ws))
    r = client.get(path(stub, RID), headers=hdr(token, write=False))
    assert r.status_code == 404
    assert link.calls == []


def test_the_generic_hand_off_routes_stay_gone(client, token, stub, use):
    use(Link(stub.ws))
    for method in ("post", "get", "delete"):
        rest = "" if method == "post" else "/" + RID
        r = client.request(method.upper(), f"/api/w/{stub.ws}/handoff{rest}", json={}, headers=hdr(token))
        assert r.status_code in (404, 405), (method, r.status_code)
    assert not [k for k in TABLE if "/handoff" in k[1]]
    assert TABLE[("POST", "/api/w/{ws}/hub/approve/prepare")][0] == REFUSED
    assert TABLE[("GET", "/api/w/{ws}/hub/approve/{request_id}")][0] == REFUSED
