import hashlib
import subprocess

import pytest
from devhost.host import StubHost

from orch_dashboard.errors import HostError


def git(path, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(path), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def rich(p1, stub):
    """A workspace with filled tickets, a question, evidence and a ticket branch in a real repository."""
    env = p1
    repo = env.tmp / "code"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "feat/one")
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "b.txt").write_text("new\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "work")
    s = env.store
    s.append(
        env.person_event(
            env.owner,
            "workspace",
            "settings.changed",
            set={"repos": {"code": {"path": str(repo), "default_branch": "main"}}},
        ),
        log="workspace",
    )
    ids = {}
    for title, kind in [("Second", "bug"), ("Epic one", "epic"), ("Third", "feature")]:
        env.tick()
        ids[title] = env.new_ticket(title, kind)
    first = next(t.uid for t in s.state.tickets.values() if t.key == "DEMO-0001")
    ids["First"] = first
    env.tick()
    env.fill(first)
    env.tick()
    env.update(
        first,
        {
            "ticket.links": {"repos": ["code"], "branches": {"code": "feat/one"}, "prs": [], "external": []},
            "ticket.labels": ["api", "ui"],
            "ticket.priority": "high",
        },
    )
    from orch.store import observe

    observe.observe(s, first)
    env.tick()
    env.log(first, "hello\nworld")
    return env, ids


def rows(stub, **args):
    return stub.call("ticket.query", args)["data"]


def test_query_filters_and_paging_are_snapshot_stable(rich, stub):
    env, ids = rich
    first = rows(stub, sort="key", order="asc", limit=2)
    assert [r["key"] for r in first["rows"]] == ["DEMO-0001", "DEMO-0002"] and first["total"] == 4
    env.tick()
    env.new_ticket("Added between pages")  # a later write never moves a row across pages
    second = rows(stub, sort="key", order="asc", limit=2, page=first["next_page"])
    assert [r["key"] for r in second["rows"]] == ["DEMO-0003", "DEMO-0004"] and second["next_page"] is None
    assert second["total"] == 4 and second["sub_cursor"] == first["sub_cursor"]
    assert rows(stub)["total"] == 5
    assert [r["key"] for r in rows(stub, label=["api"])["rows"]] == ["DEMO-0001"]
    assert [r["key"] for r in rows(stub, type=["bug", "epic"], sort="key", order="asc")["rows"]] == [
        "DEMO-0002",
        "DEMO-0003",
    ]
    assert [r["key"] for r in rows(stub, q="hello")["rows"]] == []  # log text is not searched, sections and titles are
    assert [r["key"] for r in rows(stub, q="req")["rows"]] == ["DEMO-0001"]
    assert rows(stub, status=["open"])["total"] == 5 and rows(stub, status=["done", "backlog"])["total"] == 0
    assert rows(stub, sort="priority")["rows"][0]["key"] == "DEMO-0001"
    assert rows(stub, owner=stub.person)["total"] == 5 and rows(stub, assignee=stub.person)["total"] == 0
    assert rows(stub, updated_since="2999-01-01T00:00:00Z")["total"] == 0
    assert rows(stub, needs_me=True)["total"] == len([r for r in rows(stub)["rows"] if r["needs"]])


def test_epic_and_parent_filters(rich, stub):
    env, ids = rich
    s = env.store
    child = ids["Third"]
    env.update(child, {"ticket.parent": "DEMO-0003"})
    got = rows(stub, epic="DEMO-0003")["rows"]
    assert [r["key"] for r in got] == ["DEMO-0004"] and got[0]["epic"] == "DEMO-0003"
    assert [r["key"] for r in rows(stub, parent="DEMO-0003")["rows"]] == ["DEMO-0004"]
    with pytest.raises(HostError, match="not_found"):
        rows(stub, epic="DEMO-0099")
    assert s.ticket("DEMO-0003").type == "epic"


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 201},
        {"limit": 0},
        {"sort": "status"},
        {"status": ["nope"]},
        {"status": []},
        {"label": "api"},
        {"assignee": "x"},
        {"parent": "../../x"},
        {"q": ""},
        {"needs_me": "yes"},
        {"file": "/etc/passwd"},
        {"page": ""},
    ],
)
def test_query_refuses_untyped_input(stub, args):
    with pytest.raises(HostError, match="invalid.input"):
        stub.call("ticket.query", args)


def test_page_tokens_are_bound_and_expire(rich, stub):
    first = rows(stub, limit=1, sort="key")
    with pytest.raises(HostError, match="invalid.input"):
        rows(stub, limit=1, sort="updated", page=first["next_page"])  # another query
    with pytest.raises(HostError, match="page.expired"):
        rows(stub, limit=1, sort="key", page="nosuch.1")
    sid = first["next_page"].partition(".")[0]
    stub.pages.snapshots[sid]["expires"] = 0
    with pytest.raises(HostError, match="page.expired"):
        rows(stub, limit=1, sort="key", page=first["next_page"])


def test_ticket_get_is_structured(rich, stub):
    got = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]
    assert got["doc"]["key"] == "DEMO-0001" and got["doc"]["acceptance"][0]["id"] == "AC1"
    assert got["doc"]["sections"]["requirements"] == "req"
    types = [e["type"] for e in got["log"]]
    assert types[0] == "ticket.created" and "log.added" in types and "branch.pushed" in types
    assert all(set(e) == {"seq", "type", "at", "actor", "summary"} for e in got["log"])
    assert set(got["log"][0]["actor"]) == {"kind", "id"} and len(got["log"][0]["summary"]) <= 200
    assert got["log_more"] is False and got["sub_cursor"]
    assert set(got["gates"]["requirements"]) == {"state", "gen", "hash12"}
    assert got["gates"]["requirements"]["state"] == "open"
    assert [c["id"] for c in got["criteria"]] == ["AC1"]
    paged = stub.call("ticket.get", {"ref": "DEMO-0001", "log_limit": 2})["data"]
    assert len(paged["log"]) == 2 and paged["log_more"] is True
    rest = stub.call("ticket.get", {"ref": "DEMO-0001", "log_since": paged["log"][-1]["seq"]})["data"]
    assert rest["log"][0]["seq"] == paged["log"][-1]["seq"] + 1
    for bad in [
        {"ref": "nope"},
        {"ref": "DEMO-0099"},
        {"ref": "DEMO-0001", "log_limit": 501},
        {"ref": "DEMO-0001", "x": 1},
    ]:
        with pytest.raises(HostError):
            stub.call("ticket.get", bad)


def test_needs_takes_only_own_person(rich, stub):
    assert stub.call("needs", {"person": stub.person})["data"]["items"] == stub.call("needs", {})["data"]["items"]
    with pytest.raises(HostError, match="op.not_allowed"):
        stub.call("needs", {"person": "p_other"})
    with pytest.raises(HostError, match="invalid.input"):
        stub.call("needs", {"x": 1})


def test_diff_is_bound_to_the_observed_source(rich, stub):
    env, ids = rich
    data = stub.call("diff", {"ref": "DEMO-0001"})["data"]
    (source,) = data["sources"]
    assert source["repo"] == "code" and source["branch"] == "feat/one"
    assert len(source["head_sha"]) == 40 and source["base_sha"] != source["head_sha"]
    assert {f["path"]: (f["status"], f["add"]) for f in data["files"]} == {"a.txt": ("M", 1), "b.txt": ("A", 1)}
    assert "+two" in data["unified"] and data["truncated"] is False
    small = stub.call("diff", {"ref": "DEMO-0001", "max_bytes": 1024})["data"]
    assert small["truncated"] is False
    # The branch moves after the host observed it: refused, not shown as the signed commit.
    repo = env.tmp / "code"
    (repo / "c.txt").write_text("late\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "late")
    with pytest.raises(HostError, match="observe.unavailable"):
        stub.call("diff", {"ref": "DEMO-0001"})
    with pytest.raises(HostError, match="invalid.input"):
        stub.call("diff", {"ref": "DEMO-0001", "repo": "elsewhere"})
    assert stub.call("diff", {"ref": "DEMO-0002"})["data"] == {
        "sources": [],
        "files": [],
        "unified": "",
        "truncated": False,
    }
    with pytest.raises(HostError, match="not_found"):
        stub.call("diff", {"ref": "DEMO-0099"})


def test_diff_truncates_on_a_line_boundary(rich, stub):
    data = stub.call("diff", {"ref": "DEMO-0001", "context": 10, "max_bytes": 1024})["data"]
    assert data["truncated"] is False  # small diff
    env, ids = rich
    repo = env.tmp / "code"
    (repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(400)))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "big")
    from orch.store import observe

    observe.observe(env.store, ids["First"])
    big = stub.call("diff", {"ref": "DEMO-0001", "max_bytes": 1024})["data"]
    assert big["truncated"] is True and len(big["unified"].encode()) <= 1024 and big["unified"].endswith("\n")


def test_artifact_get_is_chunked_and_checked(rich, stub):
    env, ids = rich
    uid = ids["First"]
    data = b"evidence bytes " * 10
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    env.store.append(
        {
            "type": "artifact.added",
            "actor": env.agent,
            "name": "out.log",
            "kind": "log",
            "sha256": digest,
            "bytes": len(data),
            "ac": "AC1",
        },
        log=uid,
        artifacts={"out.log": data},
    )
    got = stub.call("artifact.get", {"ref": "DEMO-0001", "name": "out.log", "offset": 0, "length": 100})["data"]
    import base64

    assert base64.b64decode(got["b64"]) == data[:100] and got["last"] is False and got["size"] == len(data)
    tail = stub.call("artifact.get", {"ref": "DEMO-0001", "name": "out.log", "offset": 100})["data"]
    assert base64.b64decode(tail["b64"]) == data[100:] and tail["last"] is True
    crit = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]["criteria"][0]["evidence"]
    assert crit and crit[0]["artifact"] == "out.log" and crit[0]["attested_by"] in ("agent", "person")
    for args in [{"name": "../../config.json", "offset": 0}, {"name": "missing.log", "offset": 0}]:
        with pytest.raises((HostError,)):
            stub.call("artifact.get", {"ref": "DEMO-0001", **args})
    with pytest.raises(HostError, match="invalid.input"):
        stub.call("artifact.get", {"ref": "DEMO-0001", "name": "out.log", "offset": 9999})


def test_restricted_ticket_is_invisible_to_every_read(rich, stub):
    env, ids = rich
    from orch import crypto

    from tests.store.helpers import Person

    other = Person()
    env.store.append(
        env.person_event(
            env.owner,
            "workspace",
            "member.added",
            person=other.ref,
            name="Other",
            role="member",
            pk_pub=crypto.b64u(other.pk_pub),
            device_cert=other.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )
    hidden = ids["Second"]
    env.store.append(
        env.person_event(env.owner, hidden, "visibility.changed", visibility={"restricted": [env.owner.ref]}),
        log=hidden,
    )
    assert "DEMO-0002" in [r["key"] for r in rows(stub)["rows"]]  # the owner named in the list still sees it
    stranger = StubHost(env.root, env.host_state, other.ref)
    got = stranger.call("ticket.query", {})["data"]
    assert "DEMO-0002" not in [r["key"] for r in got["rows"]] and got["total"] == 3
    for op, args in [
        ("ticket.get", {"ref": "DEMO-0002"}),
        ("ticket.get", {"ref": hidden}),
        ("diff", {"ref": "DEMO-0002"}),
        ("artifact.get", {"ref": "DEMO-0002", "name": "x", "offset": 0}),
    ]:
        with pytest.raises(HostError, match="not_found"):
            stranger.call(op, args)
    with pytest.raises(HostError, match="not_found"):
        stranger.call("ticket.query", {"parent": "DEMO-0002"})
    assert "Second" not in repr(stranger.call("needs", {})["data"])
    assert "Second" not in repr(got)
