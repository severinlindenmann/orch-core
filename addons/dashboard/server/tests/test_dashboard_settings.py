"""#306 (M4) server: the reduced workspace settings reads and their hand-offs. The server forwards what the host gives
this person, never more; every write is a typed hand-off prepared by the host and signed elsewhere."""

import base64
import json
import time

import pytest
from team import add_member, issue_grant

from orch_dashboard import adapters
from orch_dashboard.hostlink import HostLink

IDEM = "a_{:0>26}"


def idem(n):
    return IDEM.format(n)


@pytest.fixture
def team(p1):
    env = p1
    env.clock[0] = int(time.time())  # grants are judged at the stub's clock (now): issue them now
    people = {"mara": add_member(env, "Mara", "maintainer"), "tom": add_member(env, "Tom", "member")}
    people["vic"] = add_member(env, "Vic", "viewer")
    grants = {
        "owner": env.grant_id,
        "mara": issue_grant(env, people["mara"]),
        "tom": issue_grant(env, people["tom"], scope="workable"),
    }
    return env, people, grants


def handoff(world, body, n=1):
    return world.post(f"/api/w/{world.stub.ws}/handoff", {**body})


# ---- reads ----------------------------------------------------------------------------------------------------------
def test_reads_need_a_session(client, stub, p1):
    for path in ("settings", "members", "grants"):
        assert client.get(f"/api/w/{stub.ws}/{path}").status_code == 401


def test_the_owner_reads_the_settings_with_defaults_marked_and_the_repo_path_the_host_sent(worlds, team, p1):
    env, people, grants = team
    env.tick()
    env.store.append(
        env.person_event(
            env.owner,
            "workspace",
            "settings.changed",
            set={
                "grant_hours": 12,
                "repos": {
                    "code": {
                        "path": "/srv/secret-place/code",
                        "remote": "https://git.example.test/a/code",
                        "default_branch": "main",
                    }
                },
            },
        ),
        log="workspace",
    )
    world = worlds(env.owner.ref)
    got = world.get(f"/api/w/{world.stub.ws}/settings").json()
    assert got["available"] is True
    assert (got["grant_hours"], got["claim_ttl_min"], got["lease_ttl_min"]) == (12, 120, 60)
    assert got["defaults"] == ["claim_ttl_min", "lease_ttl_min"]  # only the unset ones are marked as defaults
    assert set(got["gates"]) == {"requirements", "plan", "verify", "code"}
    assert got["gates"]["code"]["independent"] is True and "assignees" in got["gates"]["code"]["not"]
    (repo,) = got["repos"]
    assert (repo["name"], repo["default_branch"], repo["remote"]) == ("code", "main", "https://git.example.test/a/code")
    assert repo["path"] == "/srv/secret-place/code"  # the host sent it to an owner; a member never gets it (below)
    assert got["settings_at"]["by"] == env.owner.ref and got["policy_at"] is None


def test_a_member_gets_names_and_branches_only(worlds, team):
    env, people, grants = team
    env.tick()
    env.store.append(
        env.person_event(
            env.owner,
            "workspace",
            "settings.changed",
            set={
                "repos": {
                    "code": {
                        "path": "/srv/x/code",
                        "remote": "https://git.example.test/a/code",
                        "default_branch": "main",
                    }
                }
            },
        ),
        log="workspace",
    )
    world = worlds(people["tom"].ref)
    got = world.get(f"/api/w/{world.stub.ws}/settings").json()
    assert got["repos"] == [
        {"name": "code", "default_branch": "main", "remote": None, "path": None, "check": None, "tickets": None}
    ]
    assert "/srv/x" not in json.dumps(got) and "git.example.test" not in json.dumps(got)


def test_a_host_without_the_settings_cap_says_unavailable_and_nothing_is_guessed(worlds, team):
    env, people, grants = team
    world = worlds(env.owner.ref, settings_cap=False)
    got = world.get(f"/api/w/{world.stub.ws}/settings").json()
    assert got == {
        "available": False,
        "grant_hours": None,
        "claim_ttl_min": None,
        "lease_ttl_min": None,
        "defaults": None,
        "gates": None,
        "repos": None,
        "repos_more": False,
        "settings_at": None,
        "policy_at": None,
    }
    assert "settings_read" not in world.get("/api/session").json()["features"]


def test_the_repo_adapter_strips_credentials_and_forwards_only_the_path_and_check_the_host_sent():
    first = {
        "settings": {
            "grant_hours": 8,
            "claim_ttl_min": 120,
            "lease_ttl_min": 60,
            "repos": {
                "a": {"default_branch": "main", "path": "/p/a", "remote": "https://user:tok@git.example.test/x/a"},
                "b": {"default_branch": None, "remote": "ssh://git@git.example.test/x/b"},
                "c": {"default_branch": "dev", "remote": "local:c"},
            },
        },
        "next_page": None,
        "gates": {},
        "defaults": [],
    }
    got = adapters.settings.settings(first, [], False)
    assert [(r.name, r.remote) for r in got.repos] == [
        ("a", "https://git.example.test/x/a"),
        ("b", None),
        ("c", "local:c"),
    ]
    assert "tok" not in got.model_dump_json()
    assert [r.path for r in got.repos] == ["/p/a", None, None]
    first["settings"]["repos"]["a"]["check"] = {"state": "present", "branch": "main"}
    assert adapters.settings.settings(first, [], False).repos[0].check.branch == "main"
    for bad in ({"state": "odd"}, {"state": "present", "branch": f"ma{chr(0x202E)}in"}, "present"):
        first["settings"]["repos"]["a"]["check"] = bad
        with pytest.raises(Exception):
            adapters.settings.settings(first, [], False)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["settings"].update(grant_hours=169),
        lambda d: d["settings"].update(claim_ttl_min=14),
        lambda d: d["settings"].update(lease_ttl_min="60"),
        lambda d: d.update(gates={"requirements": {"approvers": ["owner"]}}),
        lambda d: d.update(gates={"nope": {}}),
        lambda d: d.update(defaults=["other"]),
        lambda d: d["settings"]["repos"].update({"Bad Name": {"default_branch": None}}),
        lambda d: d.update(settings_at={"seq": 1}),
    ],
)
def test_a_settings_answer_that_breaks_the_contract_is_refused(mutate):
    first = {
        "settings": {"grant_hours": 8, "claim_ttl_min": 120, "lease_ttl_min": 60, "repos": {}},
        "next_page": None,
        "gates": {},
        "defaults": [],
    }
    mutate(first)
    with pytest.raises(Exception) as caught:
        adapters.settings.settings(first, [], False)
    assert getattr(caught.value, "error", {}).get("code") == "internal"


def test_the_members_read_has_devices_and_no_last_seen(worlds, team):
    env, people, grants = team
    world = worlds(env.owner.ref)
    got = world.get(f"/api/w/{world.stub.ws}/members").json()
    assert [m["name"] for m in got] == ["Owner", "Mara", "Tom", "Vic"]
    assert all(
        set(m) == {"person", "name", "role", "devices", "name_hidden"} for m in got
    )  # no last_seen, nothing invented
    device = got[1]["devices"][0]
    assert set(device) == {
        "id",
        "auth",
        "scopes",
        "valid",
        # v1.9 read_ext/3 and H12d-2: the device fields the host sends (none invented)
        "label",
        "label_hidden",
        "paired_at",
        "relay",
        "cert_expires_at",
        "reader",
    }
    assert device["valid"] is True and "operate" in device["scopes"]
    assert device["reader"] is None and device["label"] is None and device["relay"] is False


def test_an_owner_sees_every_grant_and_anyone_else_their_own(worlds, team):
    env, people, grants = team
    owner, tom = worlds(env.owner.ref), worlds(people["tom"].ref)
    seen = lambda w: sorted(g["id"] for g in w.get(f"/api/w/{w.stub.ws}/grants").json())  # noqa: E731
    # The owner's own bootstrap grant ended long ago (the test clock): only active grants are listed by default.
    assert seen(owner) == sorted([grants["mara"], grants["tom"]])
    assert seen(tom) == [grants["tom"]]
    mara = worlds(people["mara"].ref)
    assert seen(mara) == [grants["mara"]]  # a maintainer is not an owner


def test_a_grant_carries_its_label_from_grant_list(worlds, team):
    """H11c-2: `grant.list` items name the grant.issued label (`runner` marks the runner grant)."""
    env, people, grants = team
    owner = worlds(env.owner.ref)
    rows = {g["id"]: g for g in owner.get(f"/api/w/{owner.stub.ws}/grants").json()}
    assert rows[grants["tom"]]["label"] == "tests"


@pytest.mark.parametrize(
    ("label", "shown"),
    [("runner", "runner"), (None, None), ("Not A Label", None), ("x" * 33, None), (7, None), ("\u202erunner", None)],
)
def test_a_grant_label_is_shown_only_as_the_schema_allows(label, shown):
    row = {"id": "gr_" + "0" * 26, "person": "p_a", "scope": "all", "verbs": "agent", "issued_at": "t",
           "expires_at": "t", "revoked_at": None, "sessions": 0}  # fmt: skip
    if label is not None:
        row["label"] = label
    assert adapters.settings.grants({"grants": [row]}, False)[0].label == shown


def test_a_revoked_grant_has_who_and_when_only_from_a_read_ext_2_host(worlds, team):
    env, people, grants = team
    env.tick()
    env.store.append(
        env.person_event(env.owner, "workspace", "grant.revoked", grant=grants["mara"], reason="done"), log="workspace"
    )
    owner = worlds(env.owner.ref)
    assert [g["id"] for g in owner.get(f"/api/w/{owner.stub.ws}/grants").json()] == [grants["tom"]]
    rows = owner.get(f"/api/w/{owner.stub.ws}/grants?active=false").json()
    revoked = next(g for g in rows if g["id"] == grants["mara"])
    assert revoked["revoked"]["by"] == env.owner.ref and revoked["revoked"]["seq"] > 1 and revoked["session_ids"] == []
    old = worlds(env.owner.ref, ext=False)
    row = next(g for g in old.get(f"/api/w/{old.stub.ws}/grants?active=false").json() if g["id"] == grants["mara"])
    assert row["revoked"] == {"at": revoked["revoked"]["at"], "by": None, "seq": None}  # unknown, not made up
    assert row["session_ids"] is None and row["sessions_more"] is None


@pytest.mark.parametrize("query", ["active=maybe", "extra=1", "active=true&active=false&x=1"])
def test_the_grants_query_is_closed(worlds, team, query):
    world = worlds(team[0].owner.ref)
    assert world.get(f"/api/w/{world.stub.ws}/grants?{query}").status_code == 400


def test_the_session_lists_the_settings_features(worlds, team):
    env, people, grants = team
    got = worlds(env.owner.ref).get("/api/session").json()["features"]
    for feature in ("settings", "members", "settings_read", "grant_list", "read_ext2"):
        assert feature in got
    assert {f for f in got if f.startswith("do:")} == {
        f"do:{op}"
        for op in ("settings.set", "policy.set", "member.add", "member.role", "member.remove", "device.remove", "grant.revoke")
    }


def test_an_op_the_host_does_not_list_is_not_offered_and_is_refused(worlds, team):
    env, people, grants = team
    world = worlds(env.owner.ref, write_ops={"member.role"})
    features = world.get("/api/session").json()["features"]
    assert "do:member.role" in features and "do:policy.set" not in features and "do:settings.set" not in features
    res = handoff(world, {"action": "settings_set", "grant_hours": 12})
    assert res.status_code == 501 and res.json()["error"]["code"] == "op.unsupported"
    # Opus RH review L2: `move` too is refused here when the host doesn't list it, not passed on for the host to refuse.
    res = handoff(world, {"action": "move", "key": "DEMO-0001", "to": "open"})
    assert res.status_code == 501 and res.json()["error"]["code"] == "op.unsupported"
    assert world.stub.requests.table == {}


# ---- hand-offs -------------------------------------------------------------------------------------------------------
@pytest.fixture
def host_calls(monkeypatch):
    seen = []
    original = HostLink.call

    def spy(self, op, args=None, **kw):
        seen.append((op, args, kw))
        return original(self, op, args, **kw)

    monkeypatch.setattr(HostLink, "call", spy)
    return seen


def test_settings_set_prepares_one_settings_changed_event_from_the_hosts_material(worlds, team, host_calls):
    env, people, grants = team
    world = worlds(env.owner.ref)
    res = handoff(world, {"action": "settings_set", "grant_hours": 12, "lease_ttl_min": 90})
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["op"] == "settings.set" and got["key"] is None and got["state"] == "prepared"
    assert got["events"][0]["type"] == "settings.changed" and got["events"][0]["set"] == {
        "grant_hours": 12,
        "lease_ttl_min": 90,
    }
    assert got["material"]["ticket"] is None and got["material"]["before"] == {"grant_hours": 8, "lease_ttl_min": 60}
    prepare = next(c for c in host_calls if c[0] == "human.prepare")
    assert prepare[1] == {
        "op": "settings.set",
        "args": {"set": {"grant_hours": 12, "lease_ttl_min": 90}},
        "handoff": True,
    }
    assert prepare[2].get("files") is None and all(c[0] != "human.submit" for c in host_calls)
    assert world.stub.ws and env.store.state.workspace.settings["grant_hours"] == 8  # nothing was written


def test_runner_grant_goes_through_the_terminal_handoff_with_runner_set(worlds, team, host_calls):
    """B11-P1: on a terminal-approval workspace the runner grant is a hand-off like any other signature."""
    env, people, grants = team
    world = worlds(env.owner.ref)
    # The dev stub serves no `grant` op (the host does): what matters is the request this server sends.
    got = handoff(world, {"action": "runner_grant", "hours": 24, "scope": "workable"}, 1)
    assert got.status_code != 422  # the action is part of the hand-off schema now
    ops = [(c[1]["op"], c[1]["args"]) for c in host_calls if c[0] == "human.prepare"]
    assert ops == [("grant", {"hours": 24, "scope": "workable", "runner": True})]


@pytest.mark.parametrize(
    "change",
    [
        {"label": "other"},
        {"hours": 25},
        {"verbs": "all"},
        {"scope": "all"},
        {"extra": 1},
        {"actor": {"kind": "person", "id": "p_other"}},
    ],
)
def test_a_prepared_runner_grant_that_differs_from_the_request_is_refused(change):
    """B11-P1: a host-prepared grant.issued with another label, hours, verbs, scope, field or person is never shown."""
    from orch_dashboard.handoff import RunnerGrant, expected

    good = {
        "type": "grant.issued",
        "label": "runner",
        "verbs": "agent",
        "hours": 24,
        "scope": "workable",
        "actor": {"kind": "person", "id": "p_me"},
    }
    check = expected(RunnerGrant(action="runner_grant", hours=24, scope="workable"), "p_me")
    assert check([good])
    assert not check([{**good, **change}])


@pytest.mark.parametrize(
    "body",
    [
        {"action": "settings_set"},
        {"action": "settings_set", "grant_hours": 0},
        {"action": "settings_set", "grant_hours": 169},
        {"action": "settings_set", "claim_ttl_min": 14},
        {"action": "settings_set", "claim_ttl_min": 1441},
        {"action": "settings_set", "lease_ttl_min": 4},
        {"action": "settings_set", "lease_ttl_min": 1441},
        {"action": "settings_set", "grant_hours": 1.5},
        {"action": "settings_set", "grant_hours": True},
        {"action": "settings_set", "grant_hours": "8"},
        {"action": "settings_set", "grant_hours": 8, "repos": {"code": {"path": "/x"}}},
        {"action": "settings_set", "repos": {"code": None}},
        {"action": "settings_set", "grant_hours": 8, "set": {"grant_hours": 9}},
    ],
)
def test_settings_set_is_typed_and_refuses_repos(worlds, team, body):
    world = worlds(team[0].owner.ref)
    res = handoff(world, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert "/x" not in res.text and world.stub.requests.table == {}


def policy(**kw):
    base = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
    return {**base, **kw}


def test_policy_set_sends_the_canonical_policy_of_one_gate(worlds, team, host_calls):
    env, people, grants = team
    world = worlds(env.owner.ref)
    res = handoff(
        world,
        {
            "action": "policy_set",
            "gate": "plan",
            "policy": policy(
                approvers=["owner", "maintainer", "owner"], count=2, **{"not": ["assignees", "assignees"]}
            ),
        },
    )
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["events"][0]["type"] == "policy.changed"
    assert got["events"][0]["gates"] == {
        "plan": {
            "approvers": ["maintainer", "owner"],
            "count": 2,
            "not": ["assignees"],
            "applies": "all",
            "independent": False,
        }
    }
    assert next(c for c in host_calls if c[0] == "human.prepare")[1]["args"] == {"gates": got["events"][0]["gates"]}
    assert got["material"]["kind"] == "payload" and got["material"]["ticket"] is None


def test_the_code_gate_is_never_approved_by_assignees_and_always_independent(worlds, team):
    world = worlds(team[0].owner.ref)
    ok = policy(
        approvers=["maintainer", "owner"], applies=["bug", "feature"], independent=True, **{"not": ["assignees"]}
    )
    res = handoff(world, {"action": "policy_set", "gate": "code", "policy": ok}, 1)
    assert res.status_code == 200 and res.json()["events"][0]["gates"]["code"]["applies"] == ["bug", "feature"]
    for bad in (
        policy(independent=True),  # not lacks assignees
        policy(independent=False, **{"not": ["assignees"]}),  # not independent
    ):
        res = handoff(world, {"action": "policy_set", "gate": "code", "policy": bad}, 2)
        assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"


@pytest.mark.parametrize(
    "body",
    [
        {"gate": "nope", "policy": policy()},
        {"gate": "plan"},
        {"gate": "plan", "policy": policy(approvers=[])},
        {"gate": "plan", "policy": policy(approvers=["everyone"])},
        {"gate": "plan", "policy": policy(approvers=["owner"] * 9)},
        {"gate": "plan", "policy": policy(count=0)},
        {"gate": "plan", "policy": policy(count=10)},
        {"gate": "plan", "policy": policy(**{"not": ["nobody"]})},
        {"gate": "plan", "policy": policy(applies=[])},
        {"gate": "plan", "policy": policy(applies=["story"])},
        {"gate": "plan", "policy": policy(applies="sometimes")},
        {"gate": "plan", "policy": policy(independent="yes")},
        {"gate": "plan", "policy": {**policy(), "extra": 1}},
        {"gate": "plan", "policy": policy(), "ref": "DEMO-0001"},
        {"gate": "plan", "policy": policy(), "gates": {"plan": policy()}},
    ],
)
def test_policy_set_is_typed_and_workspace_wide(worlds, team, body):
    world = worlds(team[0].owner.ref)
    body = json.loads(json.dumps(body))
    for key in ("policy",):
        if key in body and isinstance(body[key], dict) and "not_" in body[key]:
            body[key]["not"] = body[key].pop("not_")
    res = handoff(world, {"action": "policy_set", **body})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert world.stub.requests.table == {}


def invitee(env, name="Nia"):
    from orch import crypto

    from tests.identity.helpers import Person
    from tests.store.helpers import T0

    person = Person()
    return person, crypto.b64u(person.pk_pub), json.dumps(person.cert(created=T0 * 1000))


def test_member_add_sends_the_certificate_as_a_file_and_shows_what_it_says(worlds, team, host_calls):
    env, people, grants = team
    person, pk, cert = invitee(env)
    world = worlds(env.owner.ref)
    res = handoff(world, {"action": "member_add", "name": "Nia", "role": "member", "pk": pk, "cert": cert})
    assert res.status_code == 200, res.text
    got = res.json()
    event = got["events"][0]
    assert event["type"] == "member.added" and event["person"] == person.ref and event["pk_pub"] == pk
    assert event["name"] == "Nia" and event["role"] == "member" and event["device_cert"] == json.loads(cert)
    derived = got["material"]["review"][0]["derived"]
    assert derived["cert_person"] == person.ref and derived["cert_device"] == person.device
    assert derived["cert_scopes_max"] == ["look", "decide", "operate", "type"]
    prepare = next(c for c in host_calls if c[0] == "human.prepare")
    assert prepare[1] == {
        "op": "member.add",
        "args": {"name": "Nia", "role": "member", "pk": pk, "cert": "-"},
        "handoff": True,
    }
    assert base64.b64decode(prepare[2]["files"]["cert"]["b64"]).decode() == cert  # the bytes the person uploaded
    # The certificate is never an inline argument (a host with blobs refuses that): only its logical name is.
    assert cert not in json.dumps(prepare[1]) and set(prepare[2]["files"]) == {"cert"}
    with pytest.raises(Exception) as inline:
        world.stub.call(
            "human.prepare", {"op": "member.add", "args": {**prepare[1]["args"], "cert": cert}, "handoff": True}, {}
        )
    assert inline.value.error["code"] == "blob.invalid"
    assert person.ref not in env.store.state.workspace.members  # nothing was written


@pytest.mark.parametrize(
    "what",
    [
        "pk_short",
        "pk_chars",
        "cert_not_json",
        "cert_array",
        "cert_dup",
        "cert_float",
        "cert_big",
        "name_blank",
        "name_nl",
        "role",
    ],
)
def test_member_add_validates_the_files_and_the_name(worlds, team, what):
    env, people, grants = team
    person, pk, cert = invitee(env)
    body = {"action": "member_add", "name": "Nia", "role": "member", "pk": pk, "cert": cert}
    body |= {
        "pk_short": {"pk": pk[:-1]},
        "pk_chars": {"pk": pk[:-1] + "+"},
        "cert_not_json": {"cert": "{nope"},
        "cert_array": {"cert": "[]"},
        "cert_dup": {"cert": '{"a":1,"a":2}'},
        "cert_float": {"cert": '{"a":1.5}'},
        "cert_big": {"cert": '{"a":"' + "x" * 17000 + '"}'},
        "name_blank": {"name": " "},
        "name_nl": {"name": "a\nb"},
        "role": {"role": "root"},
    }[what]
    world = worlds(env.owner.ref)
    res = handoff(world, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert world.stub.requests.table == {}


def test_member_add_with_a_certificate_of_another_key_is_refused_by_the_host(worlds, team):
    env, people, grants = team
    person, pk, cert = invitee(env)
    other, other_pk, _ = invitee(env)
    world = worlds(env.owner.ref)
    res = handoff(world, {"action": "member_add", "name": "Nia", "role": "member", "pk": other_pk, "cert": cert})
    assert res.status_code == 409 and res.json()["error"]["code"] == "member.cert_invalid"


def test_member_role_remove_and_grant_revoke_prepare_their_events(worlds, team, host_calls):
    env, people, grants = team
    world = worlds(env.owner.ref)
    got = handoff(world, {"action": "member_role", "person": people["tom"].ref, "role": "maintainer"}, 1).json()
    assert got["events"][0]["type"] == "role.changed" and got["events"][0]["role"] == "maintainer"
    got = handoff(world, {"action": "member_remove", "person": people["vic"].ref}, 2).json()
    assert got["events"][0]["type"] == "member.removed" and got["events"][0]["person"] == people["vic"].ref
    got = handoff(world, {"action": "grant_revoke", "grant": grants["tom"], "reason": "not needed"}, 3).json()
    assert got["events"][0]["type"] == "grant.revoked" and got["events"][0]["reason"] == "not needed"
    ops = [(c[0], c[1]["op"], c[1]["args"]) for c in host_calls if c[0] == "human.prepare"]
    assert ops == [
        ("human.prepare", "member.role", {"person": people["tom"].ref, "role": "maintainer"}),
        ("human.prepare", "member.remove", {"person": people["vic"].ref}),
        ("human.prepare", "grant.revoke", {"grant": grants["tom"], "reason": "not needed"}),
    ]
    assert people["vic"].ref in env.store.state.workspace.members


def test_device_remove_prepares_one_device_removed_event_and_keeps_an_owners_last_device(worlds, team, host_calls):
    """#1198 C15: `device_remove` is a typed action; the host names the device event, and an owner's last working device
    is refused with core's plain reason (the lockout guard)."""
    env, people, grants = team
    world = worlds(env.owner.ref)
    vic = people["vic"]
    got = handoff(world, {"action": "device_remove", "device": vic.device}).json()
    assert got["op"] == "device.remove" and [e["type"] for e in got["events"]] == ["device.removed"]
    assert got["events"][0]["device"] == vic.device
    prepare = next(c for c in host_calls if c[0] == "human.prepare")
    assert prepare[1]["op"] == "device.remove" and prepare[1]["args"] == {"device": vic.device}
    res = handoff(world, {"action": "device_remove", "device": env.owner.device})
    assert res.status_code == 409 and res.json()["error"]["code"] == "device.lockout"
    assert "last working device of an owner" in res.json()["error"]["message"]
    assert vic.device in {d for d, v in env.store.state.workspace.devices.items() if not v.removed}  # nothing written


@pytest.mark.parametrize(
    "body",
    [
        {"action": "device_remove", "device": "x"},
        {"action": "device_remove", "device": "d_" + "A" * 32},
        {"action": "device_remove", "device": "d_" + "a" * 31},
        {"action": "device_remove", "device": "d_" + "a" * 32, "reason": "x"},
        {"action": "device_remove"},
    ],
)
def test_device_remove_is_typed(worlds, team, body):
    world = worlds(team[0].owner.ref)
    res = handoff(world, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert world.stub.requests.table == {}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "member_role", "person": "p_x", "role": "owner"},
        {"action": "member_role", "person": "p_" + "a" * 32, "role": "boss"},
        {"action": "member_role", "person": "p_" + "a" * 32},
        {"action": "member_remove", "person": "x"},
        {"action": "member_remove", "person": "p_" + "a" * 32, "reason": "x"},
        {"action": "grant_revoke", "grant": "gr_1"},
        {"action": "grant_revoke", "grant": "gr_" + "0" * 26, "reason": ""},
        {"action": "grant_revoke", "grant": "gr_" + "0" * 26, "reason": "a\nb"},
        {"action": "grant_revoke", "grant": "gr_" + "0" * 26, "reason": "x" * 201},
        {"action": "grant", "hours": 4},  # issuing is not offered: the signer draws the secret
        {"action": "grant_issue", "scope": "all"},
    ],
)
def test_member_and_grant_actions_are_typed(worlds, team, body):
    world = worlds(team[0].owner.ref)
    res = handoff(world, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert world.stub.requests.table == {}


def test_unknown_people_and_grants_are_not_found(worlds, team):
    world = worlds(team[0].owner.ref)
    res = handoff(world, {"action": "member_role", "person": "p_" + "a" * 32, "role": "member"}, 1)
    assert res.status_code == 404
    res = handoff(world, {"action": "grant_revoke", "grant": "gr_" + "0" * 26}, 2)
    assert res.status_code == 404


def test_only_owners_prepare_settings_policy_and_roles_and_the_host_decides(worlds, team):
    env, people, grants = team
    for who in ("mara", "tom", "vic"):
        world = worlds(people[who].ref)
        for n, body in enumerate(
            (
                {"action": "settings_set", "grant_hours": 12},
                {"action": "policy_set", "gate": "plan", "policy": policy()},
                {"action": "member_role", "person": people["tom"].ref, "role": "viewer"},
            )
        ):
            res = handoff(world, body, n + 1)
            assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied", (who, body, res.text)
        assert world.stub.requests.table == {}


def test_a_maintainer_adds_members_and_viewers_but_not_owners_and_removes_the_same(worlds, team):
    env, people, grants = team
    world = worlds(people["mara"].ref)
    person, pk, cert = invitee(env)
    base = {"action": "member_add", "name": "Nia", "pk": pk, "cert": cert}
    assert handoff(world, {**base, "role": "viewer"}, 1).status_code == 200
    assert handoff(world, {**base, "role": "maintainer"}, 2).json()["error"]["code"] == "role.denied"
    assert handoff(world, {"action": "member_remove", "person": people["vic"].ref}, 3).status_code == 200
    assert handoff(world, {"action": "member_remove", "person": env.owner.ref}, 4).status_code == 403


def test_a_non_owner_revokes_only_their_own_grant(worlds, team):
    env, people, grants = team
    world = worlds(people["tom"].ref)
    assert handoff(world, {"action": "grant_revoke", "grant": grants["tom"]}, 1).status_code == 200
    res = handoff(world, {"action": "grant_revoke", "grant": grants["mara"]}, 2)
    assert res.status_code in (403, 404)  # the host decides; a viewer of others' grants does not exist for them


def test_a_host_that_prepares_another_event_is_refused_before_anything_is_shown(worlds, team, monkeypatch):
    env, people, grants = team
    world = worlds(env.owner.ref)
    original = type(world.stub.requests).prepare

    def swapped(self, store, views, args, opts, files=None):
        if args["op"] == "member.role":
            args = {**args, "args": {**args["args"], "role": "viewer"}}  # the host prepares another role
        return original(self, store, views, args, opts, files)

    monkeypatch.setattr(type(world.stub.requests), "prepare", swapped)
    res = handoff(world, {"action": "member_role", "person": people["tom"].ref, "role": "maintainer"})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"


def test_the_stub_signer_signs_settings_and_member_requests(worlds, team, tmp_path):
    from conftest import Signing

    env, people, grants = team
    world = worlds(env.owner.ref)
    signing = Signing(world.stub, env, tmp_path)  # the signer talks to the same stub as the dashboard server
    got = handoff(world, {"action": "settings_set", "grant_hours": 12}).json()
    status, shown = signing.sign(got["code"])
    assert status == 0, shown
    env.store.refresh()
    assert env.store.state.workspace.settings["grant_hours"] == 12
    assert shown[0][1] == f"DEMO~{world.stub.ws[-8:]}/SETTINGS"  # stream A: <PREFIX>~<WS8>/SETTINGS
    person, pk, cert = invitee(env)
    got = handoff(world, {"action": "member_add", "name": "Nia", "role": "viewer", "pk": pk, "cert": cert}, 2).json()
    status, shown = signing.sign(got["code"], confirm=lambda text, word: word == person.ref)
    assert status == 0, shown
    env.store.refresh()
    assert env.store.state.workspace.members[person.ref].role == "viewer"
    got = handoff(world, {"action": "grant_revoke", "grant": grants["owner"]}, 3).json()
    assert signing.sign(got["code"])[0] == 0
    env.store.refresh()
    assert env.store.state.workspace.grants[grants["owner"]].revoked
    after = world.get(f"/api/w/{world.stub.ws}/settings").json()
    assert after["grant_hours"] == 12 and after["defaults"] == ["claim_ttl_min", "lease_ttl_min"]


BIDI = "\u202e"


@pytest.mark.parametrize("hidden", ["\u202e", "\u2066", "\u200b", "\u200f", "\ufeff", "\u2028"])
def test_hidden_characters_from_the_host_are_not_forwarded(hidden):
    page = {
        "settings": {
            "grant_hours": 8,
            "claim_ttl_min": 120,
            "lease_ttl_min": 60,
            "repos": {"a": {"default_branch": "main", "remote": f"https://git.example.test/x{hidden}moc/a"}},
        },
        "next_page": None,
        "gates": {},
        "defaults": [],
    }
    assert adapters.settings.settings(page, [], False).repos[0].remote is None  # not repaired, not shown
    page["settings"]["repos"] = {"a": {"default_branch": f"ma{hidden}in"}}
    with pytest.raises(Exception) as caught:
        adapters.settings.settings(page, [], False)
    assert caught.value.error["code"] == "internal"


def test_roster_and_grant_words_are_checked_and_a_bad_value_is_internal_not_500():
    good = {
        "members": [
            {
                "person": "p_a",
                "name": "N",
                "role": "member",
                "devices": [{"id": "d_1", "auth": None, "scopes": ["look"], "valid": True}],
            }
        ]
    }
    assert adapters.settings.roster(good)[0].devices[0].scopes == ["look"]
    for patch in ({"scopes": ["lo" + BIDI + "ok"]}, {"id": "d_" + BIDI}, {"auth": "pass" + BIDI}):
        bad = {"members": [{**good["members"][0], "devices": [{**good["members"][0]["devices"][0], **patch}]}]}
        with pytest.raises(Exception) as caught:
            adapters.settings.roster(bad)
        assert caught.value.error["code"] == "internal"
    grant = {
        "id": "gr_" + "0" * 26,
        "person": "p_a",
        "scope": "all",
        "verbs": "agent",
        "issued_at": "x",
        "expires_at": "y",
        "sessions": 0,
    }
    with pytest.raises(Exception) as caught:
        adapters.settings.grants({"grants": [{**grant, "scope": "al" + BIDI + "l"}]}, False)
    assert caught.value.error["code"] == "internal"
    with pytest.raises(Exception) as caught:  # a wrong type pydantic rejects is the same error, not a 500
        adapters.settings.grants({"grants": [{**grant, "person": 5}]}, False)
    assert caught.value.error["code"] == "internal"


@pytest.mark.parametrize("field,value", [("name", "Ni" + BIDI + "a"), ("reason", "x" + BIDI)])
def test_typed_one_line_texts_refuse_bidi_and_invisible_characters(worlds, team, field, value):
    world = worlds(team[0].owner.ref)
    if field == "name":
        person, pk, cert = invitee(team[0])
        body = {"action": "member_add", "name": value, "role": "member", "pk": pk, "cert": cert}
    else:
        body = {"action": "grant_revoke", "grant": "gr_" + "0" * 26, field: value}
    res = handoff(world, body)
    assert res.status_code == 400


def test_repo_set_and_remove_are_typed_settings_changes(worlds, team, host_calls, tmp_path):
    world = worlds(team[0].owner.ref)
    (tmp_path / "api" / ".git").mkdir(parents=True)
    path = str(tmp_path / "api")
    res = handoff(world, {"action": "repo_set", "name": "api", "path": "/no/such/folder"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"  # the host looks at the path
    res = handoff(world, {"action": "repo_set", "name": "api", "path": path, "default_branch": "main"})
    assert res.status_code == 200, res.text
    want = {"repos": {"api": {"path": path, "default_branch": "main"}}}
    assert res.json()["events"][0]["type"] == "settings.changed" and res.json()["events"][0]["set"] == want
    assert [c for c in host_calls if c[0] == "human.prepare"][-1][1]["args"] == {"set": want}
    res = handoff(world, {"action": "repo_remove", "name": "api"}, 2)
    assert res.status_code == 200, res.text
    assert res.json()["events"][0]["set"] == {"repos": {"api": None}}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "repo_set", "name": "Api", "path": "/x"},
        {"action": "repo_set", "name": "api", "path": "relative/dir"},
        {"action": "repo_set", "name": "api", "path": ""},
        {"action": "repo_set", "name": "api", "path": "/x\u202ey"},
        {"action": "repo_set", "name": "api", "path": "/x", "remote": "https://user:tok@git.example.test/a"},
        {"action": "repo_set", "name": "api", "path": "/x", "remote": "git@git.example.test:a.git"},
        {"action": "repo_set", "name": "api", "path": "/x", "remote": "http://git.example.test/a"},
        {"action": "repo_set", "name": "api", "path": "/x", "extra": 1},
        {"action": "repo_set", "name": "api"},
        {"action": "repo_remove", "name": "../x"},
        {"action": "repo_remove"},
    ],
)
def test_repo_actions_are_closed_and_typed(worlds, team, body):
    world = worlds(team[0].owner.ref)
    res = handoff(world, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert "tok" not in res.text and world.stub.requests.table == {}


def test_f8_the_repo_path_check_runs_before_any_request_and_says_why_in_plain_words(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from orch_dashboard.errors import HostError
    from orch_dashboard.gateway import Gateway

    home = tmp_path / "home"
    root = tmp_path / "proj"
    (root / ".orch").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    home.mkdir()
    ws = "a" * 32
    me = SimpleNamespace(_pins=lambda: {ws: {"root": str(root), "store": ".orch"}})

    def refusal(path):
        with pytest.raises(HostError) as exc:
            Gateway.check_repo_path(me, ws, path)
        return exc.value.error["message"]

    assert "home folder" in refusal(str(home))
    assert "not a git repository" in refusal(str(tmp_path / "plain"))
    assert "orch's own data folder" in refusal(str(root / ".orch"))
    assert "does not exist" in refusal(str(tmp_path / "nope"))
    Gateway.check_repo_path(me, ws, str(tmp_path / "repo"))  # a real git folder passes
    Gateway.check_repo_path(
        SimpleNamespace(_pins=lambda: {}), ws, str(home)
    )  # a workspace not known here: the host decides


def test_an_owner_gets_the_keys_of_the_tickets_that_link_a_repo_and_only_plain_keys():
    from orch_dashboard.adapters import settings as adapter

    entry = {"default_branch": "main", "path": "/x", "linked_by": ["DEMO-0001", "TQ-2", "DEMO-12345678901"]}
    assert adapter.repo("code", entry).tickets == ["DEMO-0001", "TQ-2", "DEMO-12345678901"]
    for bad in (["no key"], ["DEMO-1\n"], [f"DEMO-{i}" for i in range(13)], "DEMO-1"):
        try:
            adapter.repo("code", {**entry, "linked_by": bad})
        except Exception:
            continue
        raise AssertionError(bad)


def test_the_form_can_ask_the_path_check_before_anything_is_prepared(worlds, team, host_calls, tmp_path):
    world = worlds(team[0].owner.ref)
    (tmp_path / "api" / ".git").mkdir(parents=True)
    url = f"/api/w/{world.stub.ws}/repos/check"
    base = {"action": "repo_set", "name": "api"}
    bad = world.post(url, {**base, "path": "/no/such/folder"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid.input", bad.text
    assert world.post(url, {**base, "path": str(tmp_path / "api")}).json() == {"ok": True}
    assert world.post(url, {**base, "path": "/x", "extra": 1}).status_code == 400
    assert not world.stub.requests.table  # a dry run stores no request
    for who in ("mara", "tom", "vic"):
        other = worlds(team[1][who].ref)
        res = other.post(url, {**base, "path": str(tmp_path / "api")})
        assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied", (who, res.text)


def test_the_path_check_route_is_rate_limited_per_session(worlds, team):
    world = worlds(team[0].owner.ref)
    url = f"/api/w/{world.stub.ws}/repos/check"
    body = {"action": "repo_set", "name": "api", "path": "/no/such/folder"}
    codes = [world.post(url, body).status_code for _ in range(22)]
    assert codes[:20] == [400] * 20 and codes[20:] == [503, 503]


def test_concurrent_path_checks_cannot_pass_the_limit(worlds, team):
    from concurrent.futures import ThreadPoolExecutor

    world = worlds(team[0].owner.ref)
    url = f"/api/w/{world.stub.ws}/repos/check"
    body = {"action": "repo_set", "name": "api", "path": "/no/such/folder"}
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: world.post(url, body).status_code, range(40)))
    assert codes.count(400) == 20 and codes.count(503) == 20
