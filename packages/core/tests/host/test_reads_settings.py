"""H6b v1.5, ``workspace.settings`` (§0.8.5, cap ``settings/1``): the effective settings and default gate policies, the
per-viewer repos, ``settings_at`` / ``policy_at``, and authenticated snapshot paging."""

from __future__ import annotations

import json

import pytest

from orch.host import reads_ws
from orch.host.reads import CAPS
from tests.host.dispatch_helpers import UID, err
from tests.host.live_helpers import SESSION, world
from tests.host.live_helpers import ok as ok_
from tests.host.test_dispatch_matrix import as_agent_class, wrong_secret
from tests.tamper import tamper_text

OK_REMOTE = "https://example.org/team/proj"


def ok(res):
    return ok_(res)["data"]


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def wq(tmp_path):
    """The test UID is mapped to quinn, a plain member."""
    s = world(tmp_path, map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def settings(w, **changes):
    """A ``settings.changed`` by the owner, appended through the host's store."""
    return w.person_event("settings.changed", set=changes)


def repos(w, **named):
    return settings(w, repos={name: spec for name, spec in named.items()})


def link(w, c, key: str, *names: str) -> None:
    pair = "links=" + json.dumps({"repos": list(names)})
    ok_(c.req("set", {"ref": key, "pairs": [pair]}, grant=w.grant, session=SESSION, idem=True))


def policy(**kw):
    return {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False, **kw}


# --- the cap and the shape -------------------------------------------------------------------------------------------


def test_the_cap_settings_1_is_in_the_welcome(w):
    caps = w.client().welcome["caps"]
    assert {"settings/1", "text/1", "read_ext/1", "read_ext/2", "query/1", "activity/1"} <= set(caps)
    assert set(CAPS) <= set(caps)
    assert w.client().welcome["ops"]["workspace.settings"] == 1


def test_the_defaults_are_marked_and_the_four_gates_are_given(w):
    data = ok(w.client().req("workspace.settings"))
    assert set(data) == {
        "settings",
        "next_page",
        "gates",
        "defaults",
        "addons",
        "publish",
        "settings_at",
        "policy_at",
        "sub_cursor",
    }
    assert data["publish"] is None
    assert data["settings"] == {"grant_hours": 8, "claim_ttl_min": 120, "lease_ttl_min": 60, "repos": {}}
    assert data["defaults"] == ["grant_hours", "claim_ttl_min", "lease_ttl_min"]
    assert data["next_page"] is None and data["settings_at"] is None and data["policy_at"] is None
    assert list(data["gates"]) == ["requirements", "plan", "verify", "code"]
    for gate in data["gates"].values():
        assert set(gate) == {"approvers", "count", "not", "applies", "independent"}
        assert gate["approvers"] == sorted(gate["approvers"]) and gate["not"] == sorted(gate["not"])
    assert data["gates"]["code"]["applies"] == "off" and data["gates"]["code"]["independent"] is True
    assert data["sub_cursor"].startswith("c1.")


def test_a_set_duration_leaves_defaults_and_settings_at_names_its_event(w):
    c = w.client()
    event = settings(w, claim_ttl_min=30, grant_hours=8)  # 8 is the default value, but it was set: not a default
    data = ok(c.req("workspace.settings"))
    assert data["settings"]["claim_ttl_min"] == 30 and data["settings"]["grant_hours"] == 8
    assert data["defaults"] == ["lease_ttl_min"]
    assert data["settings_at"] == {"seq": event["seq"], "at": event["at"], "by": w.lab.owner}
    assert data["policy_at"] is None
    later = settings(w, lease_ttl_min=15)
    data = ok(c.req("workspace.settings"))
    assert data["defaults"] == [] and data["settings_at"]["seq"] == later["seq"] > event["seq"]


def test_a_workspace_policy_change_shows_in_gates_and_policy_at(w):
    c = w.client()
    plan = policy(approvers=["maintainer", "owner"], count=2)
    event = w.person_event("policy.changed", gates={"plan": plan})
    data = ok(c.req("workspace.settings"))
    assert data["gates"]["plan"] == plan and data["gates"]["requirements"]["count"] == 1
    assert data["policy_at"] == {"seq": event["seq"], "at": event["at"], "by": w.lab.owner}
    assert data["settings_at"] is None
    # a ticket's override is that ticket's (ticket.get), not the workspace default
    uid, key = w.ticket()
    w.person_event("policy.changed", uid, gates={"plan": policy(approvers=["owner"], count=1)})
    assert ok(c.req("workspace.settings"))["gates"]["plan"] == plan
    assert ok(c.req("workspace.settings"))["policy_at"]["seq"] == event["seq"]


# --- repos per viewer ------------------------------------------------------------------------------------------------


def seed_repos(w):
    repos(
        w,
        proj={"path": "/srv/proj", "remote": OK_REMOTE, "default_branch": "main"},
        bare={"path": "/srv/bare"},
    )


def test_an_owner_gets_path_and_remote_and_null_for_what_is_not_set(w):
    seed_repos(w)
    got = ok(w.client().req("workspace.settings"))["settings"]["repos"]
    assert list(got) == ["bare", "proj"]  # ordered by name
    assert {k: v for k, v in got["proj"].items() if k != "check"} == {
        "default_branch": "main",
        "path": "/srv/proj",
        "remote": OK_REMOTE,
    }
    assert {k: v for k, v in got["bare"].items() if k != "check"} == {
        "default_branch": None,
        "path": "/srv/bare",
        "remote": None,
    }
    assert got["proj"]["check"] == {"state": "missing", "branch": None}  # /srv/proj is not on this machine


def test_an_owner_is_told_which_visible_tickets_link_a_repo(w):
    seed_repos(w)
    _, key = w.ticket("links proj")
    c = w.client()
    link(w, c, key, "proj")
    got = ok(c.req("workspace.settings"))["settings"]["repos"]
    assert got["proj"]["linked_by"] == [key] and "linked_by" not in got["bare"]


def test_a_member_gets_only_the_name_and_default_branch(wq):
    seed_repos(wq)
    got = ok(wq.client().req("workspace.settings"))
    assert got["settings"]["repos"] == {"bare": {"default_branch": None}, "proj": {"default_branch": "main"}}
    assert "settings_at" in got and "policy_at" in got  # P and G always get them
    text = json.dumps(got)
    assert "/srv" not in text and OK_REMOTE not in text


def test_a_maintainer_is_served_path_and_remote_a_viewer_is_not(wq):
    seed_repos(wq)
    c = wq.client()
    wq.person_event("role.changed", person=wq.quinn.ref, role="maintainer")
    assert ok(c.req("workspace.settings"))["settings"]["repos"]["proj"]["path"] == "/srv/proj"
    wq.person_event("role.changed", person=wq.quinn.ref, role="viewer")
    assert "path" not in ok(c.req("workspace.settings"))["settings"]["repos"]["proj"]


def test_g_acting_for_an_owner_is_not_served_path_and_remote(w):
    seed_repos(w)
    c = w.client()
    assert "path" in ok(c.req("workspace.settings"))["settings"]["repos"]["proj"]  # the owner at the keyboard
    via_grant = ok(c.req("workspace.settings", grant=w.grant, session=SESSION))
    assert via_grant["settings"]["repos"] == {"bare": {"default_branch": None}, "proj": {"default_branch": "main"}}
    assert "settings_at" in via_grant and "/srv" not in json.dumps(via_grant)


def test_an_unparseable_remote_is_omitted_never_repaired(w, monkeypatch):
    seed_repos(w)
    real = reads_ws.check_repo_identity

    def refuse_proj(value):  # a stored value that is no F1 canonical identity (the model would not store one)
        if value == OK_REMOTE:
            raise reads_ws.HashError("not canonical")
        return real(value)

    monkeypatch.setattr(reads_ws, "check_repo_identity", refuse_proj)
    got = ok(w.client().req("workspace.settings"))["settings"]["repos"]
    assert {k: v for k, v in got["proj"].items() if k != "check"} == {"default_branch": "main", "path": "/srv/proj"}
    assert got["bare"]["remote"] is None


@pytest.mark.parametrize(
    "remote",
    [
        "https://user:secret@example.org/x",
        "ssh://git@example.org/x",
        "git@example.org:x.git",
        "http://example.org/x",
        "",
    ],
)
def test_the_remote_rule_is_the_f1_canonical_identity(remote):
    entry = reads_ws._repo_entry({"path": "/p", "remote": remote}, True)
    assert "remote" not in entry and entry["path"] == "/p"
    for good in ("https://example.org/a/b", "https://example.org:8443/a", "local:proj"):
        assert reads_ws._repo_entry({"path": "/p", "remote": good}, True)["remote"] == good


# --- no-person -------------------------------------------------------------------------------------------------------


def test_no_person_gets_less_and_only_repos_a_visible_ticket_links(w):
    seed_repos(w)
    repos(w, hidden={"path": "/srv/hidden", "default_branch": "dev"}, unlinked={"path": "/srv/u"})
    uid_open, open_key = w.ticket("linked in the open")
    uid_secret, secret_key = w.ticket("linked in secret")
    c = w.client()
    link(w, c, open_key, "proj")
    link(w, c, secret_key, "hidden")
    w.restrict(uid_secret, [w.lab.owner])
    settings(w, grant_hours=4)
    as_agent_class(w)
    for call in (c.req("workspace.settings"), c.req("workspace.settings", grant=wrong_secret(w.grant))):
        got = ok(call)
        assert set(got) == {
            "settings",
            "next_page",
            "gates",
            "defaults",
            "sub_cursor",
        }  # no settings_at, policy_at
        assert got["settings"]["repos"] == {"proj": {"default_branch": "main"}}  # linked by a visible ticket only
        assert got["settings"]["grant_hours"] == 4 and got["defaults"] == ["claim_ttl_min", "lease_ttl_min"]
        assert list(got["gates"]) == ["requirements", "plan", "verify", "code"]
        text = json.dumps(got)
        for secret in ("/srv", "example.org", "hidden", "unlinked", "bare", "remote", "path"):
            assert secret not in text, secret


def test_no_person_repos_follow_the_visibility_now(w):
    repos(w, a={"path": "/a"}, b={"path": "/b"})
    uid, key = w.ticket()
    c = w.client()
    link(w, c, key, "a", "b")
    as_agent_class(w)
    assert list(ok(c.req("workspace.settings"))["settings"]["repos"]) == ["a", "b"]
    w.restrict(uid, [w.lab.owner])  # the ticket is no longer visible to no-person: neither are its repos
    assert ok(c.req("workspace.settings"))["settings"]["repos"] == {}


# --- paging ----------------------------------------------------------------------------------------------------------


def _settle_pins(w, timeout: float = 120) -> None:
    """The first request after a change of the declared repos pins every one of them (F7, ``sync_git``). That took
    one fresh git reader child per repo until #471 (40 repos: 3 s here, over the clients' 10 s read deadline on
    loaded CI runners, #468) and is one child per 64 repos now; it is still a setup cost, not what the paging tests
    measure, so it is paid here under a deadline sized for it. A real hang still fails, after ``timeout``."""
    warm = w.client()
    try:
        warm.ws.send("settle", "workspace.settings")
        warm.ws.read(timeout)
    finally:
        warm.close()


def many(w, n: int = 40) -> list[str]:
    names = [f"r{i:02d}" for i in range(n)]
    for chunk in (names[: n // 2], names[n // 2 :]):  # one settings.set carries at most 32
        repos(w, **{name: {"path": f"/srv/{name}", "default_branch": "main"} for name in chunk})
    _settle_pins(w)
    return names


def test_the_repos_page_32_at_a_time_on_a_stable_snapshot(w):
    names = many(w)
    c = w.client()
    first = ok(c.req("workspace.settings"))
    assert list(first["settings"]["repos"]) == names[:32] and first["next_page"] is not None
    # a change between the pages does not change page 2: not the repos, not the durations, not the cursor
    settings(w, grant_hours=3, repos={"zzz": {"path": "/srv/zzz"}})
    w.person_event("policy.changed", gates={"plan": policy(count=2)})
    started = w.service.git_pool.started
    second = ok(c.req("workspace.settings", {"page": first["next_page"]}))
    # the changed declared set retries all 41 unpinnable /srv/<name> repos in ONE reader child, not 41 (#471, #468)
    assert w.service.git_pool.started - started == 1
    assert list(second["settings"]["repos"]) == names[32:] and second["next_page"] is None
    for key in ("gates", "defaults", "sub_cursor", "settings_at", "policy_at"):
        assert second[key] == first[key], key  # every page repeats the scalar part of the same snapshot
    assert second["settings"]["grant_hours"] == first["settings"]["grant_hours"] == 8
    assert first["settings"]["repos"].keys().isdisjoint(second["settings"]["repos"])
    fresh = ok(c.req("workspace.settings"))  # a new first page sees the change
    assert (
        fresh["settings"]["grant_hours"] == 3
        and "zzz" in ok(c.req("workspace.settings", {"page": fresh["next_page"]}))["settings"]["repos"]
    )
    assert fresh["gates"]["plan"]["count"] == 2 and fresh["settings_at"]["seq"] > first["settings_at"]["seq"]


def test_a_smaller_limit_pages_with_no_gap_and_no_repeat(w):
    names = many(w, 12)
    c = w.client()
    seen, page, rounds = [], None, 0
    while True:
        got = ok(c.req("workspace.settings", {"limit": 5, **({"page": page} if page else {})}))
        seen += list(got["settings"]["repos"])
        page, rounds = got["next_page"], rounds + 1
        if page is None:
            break
    assert seen == names and rounds == 3
    # the first page limit is the snapshot limit: a later page without one keeps it, another one is another request
    first = ok(c.req("workspace.settings", {"limit": 5}))
    assert len(ok(c.req("workspace.settings", {"page": first["next_page"]}))["settings"]["repos"]) == 5
    assert err(c.req("workspace.settings", {"page": first["next_page"], "limit": 4})) == "invalid.input"
    assert len(ok(c.req("workspace.settings", {"page": first["next_page"], "limit": 5}))["settings"]["repos"]) == 5


def test_a_page_token_that_is_forged_altered_foreign_or_old_is_page_expired(w):
    many(w)
    c = w.client()
    token = ok(c.req("workspace.settings"))["next_page"]
    assert err(c.req("workspace.settings", {"page": "p1.AAAA.BBBB"})) == "page.expired"
    assert err(c.req("workspace.settings", {"page": "garbage"})) == "page.expired"
    body, _, mac = token.rpartition(".")
    assert err(c.req("workspace.settings", {"page": body + "." + mac[::-1]})) == "page.expired"
    assert err(c.req("workspace.settings", {"page": tamper_text(body) + "." + mac})) == "page.expired"
    # another principal (the owner's grant is another principal than the person's connection)
    assert err(c.req("workspace.settings", {"page": token}, grant=w.grant, session=SESSION)) == "page.expired"
    # a token this host issued for another op (ticket.query's), for this very principal
    pages = w.service.pages
    snap = pages.open("ticket.query", f"uid:{UID}", w.service.journal.epoch, w.lab.owner, {})
    assert err(c.req("workspace.settings", {"page": pages.token(snap, 0, {})})) == "page.expired"
    assert ok(c.req("workspace.settings", {"page": token}))["settings"]["repos"]  # the real one still works
    w.lab.ws.clock[0] += 11 * 60  # ten minutes pass
    assert err(c.req("workspace.settings", {"page": token})) == "page.expired"


def test_a_token_with_other_arguments_is_invalid_input(w):
    many(w)
    c = w.client()
    token = ok(c.req("workspace.settings", {"limit": 10}))["next_page"]
    assert err(c.req("workspace.settings", {"page": token, "limit": 9})) == "invalid.input"
    assert err(c.req("workspace.settings", {"page": token, "limit": 33})) == "invalid.input"  # (schema: 1..32)
    assert err(c.req("workspace.settings", {"limit": 0})) == "invalid.input"
    assert err(c.req("workspace.settings", {"page": token, "extra": 1})) == "invalid.input"
    assert err(c.req("workspace.settings", {"page": ""})) == "invalid.input"


def test_each_page_reapplies_the_current_visibility_for_no_person(w):
    repos(w, **{n: {"path": f"/srv/{n}"} for n in ("r1", "r2", "r3", "r4")})
    tickets = {}
    c = w.client()
    for n in ("r1", "r2", "r3", "r4"):
        uid, key = w.ticket(f"links {n}")
        link(w, c, key, n)
        tickets[n] = uid
    as_agent_class(w)
    first = ok(c.req("workspace.settings", {"limit": 2}))
    assert list(first["settings"]["repos"]) == ["r1", "r2"] and first["next_page"] is not None
    w.restrict(tickets["r3"], [w.lab.owner])  # r3 becomes invisible between the pages: dropped, no hole
    second = ok(c.req("workspace.settings", {"page": first["next_page"]}))
    assert list(second["settings"]["repos"]) == ["r4"] and second["next_page"] is None
    assert second["settings"]["repos"] == {"r4": {"default_branch": None}}


def test_the_sub_cursor_of_the_snapshot_joins_sub_open_without_loss_or_repeat(w):
    c = w.client()
    before = settings(w, lease_ttl_min=15)
    data = ok(c.req("workspace.settings"))
    assert data["settings"]["lease_ttl_min"] == 15
    after = settings(w, lease_ttl_min=20)
    conn = w.conn()
    sub = conn.open(["workspace"], after=data["sub_cursor"])
    frame = conn.evt(sub["sub"])
    assert [(i["type"], i["seq"]) for i in frame["items"]] == [("settings.changed", after["seq"])]  # not `before`
    assert before["seq"] < after["seq"]
    conn.quiet(sub["sub"], 0.3)


def test_the_publish_server_is_read_by_a_person_and_not_by_an_agent(w):
    """The dashboard asks whether Publish is set up (AE4): the addresses reach a person, never the no-person column."""
    c = w.client()
    value = {"server": "https://files.example.com", "share": "https://share.example.com"}
    settings(w, publish=value)
    assert ok(c.req("workspace.settings"))["publish"] == value
    as_agent_class(w)
    assert "publish" not in ok(c.req("workspace.settings"))


def test_an_agent_with_a_grant_reads_add_on_settings_and_a_no_person_reader_does_not(w):
    """G6 ruling 5: add-on settings are not secret; an agent with a grant (column G) reads them, column N does not."""
    c = w.client()
    assert ok(c.req("workspace.settings", grant=w.grant, session=SESSION))["addons"] == {}
    as_agent_class(w)
    assert "addons" not in ok(c.req("workspace.settings", grant=wrong_secret(w.grant)))
