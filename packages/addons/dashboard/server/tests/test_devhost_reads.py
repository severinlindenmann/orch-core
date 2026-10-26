import hashlib

import pytest
from conftest import git
from devhost.host import StubHost

from orch_dashboard.errors import HostError


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


def test_altered_page_tokens_are_page_expired(rich, stub):
    first = rows(stub, limit=1, sort="key")
    sid, offset, mac = first["next_page"].split(".")
    for bad in (
        f"{sid}.{int(offset) + 1}.{mac}",
        f"{sid}.{offset}",
        f"{sid}.{offset}.{'0' * len(mac)}",
        f"{sid}.0.{mac}",
    ):
        with pytest.raises(HostError, match="page.expired"):
            rows(stub, limit=1, sort="key", page=bad)
    assert rows(stub, limit=1, sort="key", page=first["next_page"])["rows"]


def test_ticket_get_is_structured(rich, stub):
    got = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]
    assert got["doc"]["key"] == "DEMO-0001" and got["doc"]["acceptance"][0]["id"] == "AC1"
    assert got["doc"]["sections"]["requirements"] == "req"
    types = [e["type"] for e in got["log"]]
    assert types[0] == "ticket.created" and "log.added" in types and "branch.pushed" in types
    assert all(set(e) == {"seq", "type", "at", "actor", "summary", "data"} for e in got["log"])
    assert {"kind", "id"} <= set(got["log"][0]["actor"]) and len(got["log"][0]["summary"]) <= 200
    assert got["log_more"] is False and got["sub_cursor"]
    assert {"state", "gen", "hash12", "waiting", "eligible", "policy", "decisions"} <= set(got["gates"]["requirements"])
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


def test_without_read_ext_the_optional_fields_are_absent(rich, stub):
    stub.ext = False
    assert "read_ext/1" not in stub.welcome()["caps"] and "activity/1" not in stub.welcome()["caps"]
    row = rows(stub)["rows"][0]
    assert not {"restricted", "tasks", "criteria", "blocking_questions"} & set(row)
    got = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]
    assert all(set(e) == {"seq", "type", "at", "actor", "summary"} for e in got["log"])
    assert not {"status", "settled", "tasks", "verdict", "artifacts", "claim", "source", "pushes", "sections"} & set(
        got
    )
    assert set(got["gates"]["requirements"]) == {"state", "gen", "hash12"}
    item = stub.call("needs", {})["data"]["items"][0]
    assert not {"since", "text", "status", "payload"} & set(item)


def test_rows_and_ticket_carry_the_v14_fields(rich, stub):
    row = next(r for r in rows(stub)["rows"] if r["key"] == "DEMO-0001")
    assert row["restricted"] is False and row["tasks"] == {"done": 0, "skipped": 0, "total": 2}
    assert row["criteria"] == {"done": 0, "total": 1} and row["blocking_questions"] == 0 and row["claim"] is None
    got = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]
    assert got["status"] == "open" and got["settled"] is False and "closed" not in got
    assert [t["id"] for t in got["tasks"]] == ["T1", "T2"] and got["tasks"][0]["leased_by"] is None
    assert got["verdict"] is None and got["source"][0]["name"] == "code" and len(got["pushes"]) == 1
    q1, q2 = got["questions"]
    assert q1["state"] == "answered" and q1["answer"]["option"] == "csv" and q1["asked_by"]["kind"] == "agent"
    assert q2["text"] == "Any naming rule?" and q2["options"][0] == {"key": "csv", "label": "Monthly CSV"}
    assert q2["to_ref"] == "ticket_owner" and q2["to"] == [stub.person]
    created = got["log"][0]
    assert created["type"] == "ticket.created" and created["data"]["title"] == "First" or created["data"]["title"]
    assert got["log"][0]["actor"]["kind"] in ("agent", "person", "host")


def test_needs_carry_text_payload_and_who_who_can_see(rich, stub):
    items = stub.call("needs", {})["data"]["items"]
    question = next(i for i in items if i["kind"] == "question" and i["ref"] == "Q1" or i["kind"] == "question")
    assert question["status"] == "open" and question["since"] and question["text"].endswith("?")
    assert question["payload"]["options"][0]["key"] == "csv" and question["payload"]["blocking"] in (True, False)
    assert all(stub.person in i["who"] for i in items)


def test_closed_tickets_read_plainly_and_list_only_on_request(rich, stub):
    env, ids = rich
    uid = ids["Second"]
    env.tick()
    env.store.append(
        env.person_event(env.owner, uid, "ticket.closed", resolution="duplicate", duplicate_of="DEMO-0001", text="dup"),
        log=uid,
    )
    assert "DEMO-0002" not in [r["key"] for r in rows(stub)["rows"]]  # not listed without a status filter
    only = rows(stub, status=["closed"])["rows"]
    assert [r["key"] for r in only] == ["DEMO-0002"]
    assert only[0]["closed"]["resolution"] == "duplicate" and only[0]["closed"]["duplicate_of"] == "DEMO-0001"
    got = stub.call("ticket.get", {"ref": "DEMO-0002"})["data"]  # a plain read never errors
    assert got["status"] == "closed" and got["settled"] is True and got["closed"]["text"] == "dup"
    assert got["log"][-1]["type"] == "ticket.closed" and got["log"][-1]["data"]["duplicate_of"] == "DEMO-0001"
    assert stub.call("diff", {"ref": "DEMO-0002"})["data"]["files"] == []


def test_snips_are_cut_without_an_ellipsis(rich, stub):
    env, ids = rich
    uid = ids["First"]
    env.tick()
    long = "x" * 2000
    env.log(uid, long)
    got = stub.call("ticket.get", {"ref": "DEMO-0001", "log_limit": 500})["data"]
    item = next(e for e in got["log"] if e["type"] == "log.added" and len(e["data"].get("text", "")) > 900)
    assert len(item["data"]["text"].encode()) == 1024 and item["data"]["text_cut"] is True
    assert "…" not in item["data"]["text"]


def activity(stub, **args):
    return stub.call("activity.list", args)["data"]


def test_activity_pages_follow_next_page_and_tolerate_short_pages(rich, stub):
    stub.scan_budget = 4
    seen, page, pages = [], None, 0
    while True:
        data = activity(stub, limit=3, scope="tickets", **({"page": page} if page else {}))
        assert len(data["items"]) <= 3
        seen += data["items"]
        pages += 1
        page = data["next_page"]
        if not page:
            break
    assert pages > 2 and [i["jpos"] for i in seen] == sorted((i["jpos"] for i in seen), reverse=True)
    assert len({i["id"] for i in seen}) == len(seen) and all(i["key"] for i in seen)
    stub.scan_budget = None
    everything = activity(stub, limit=200)["items"]
    assert any(i["log"] == "workspace" and i["data"] for i in everything)
    assert len(everything) > len(seen)


def test_activity_tokens_are_principal_bound_and_arguments_bound(rich, stub):
    env, ids = rich
    first = activity(stub, limit=2)
    with pytest.raises(HostError, match="invalid.input"):
        activity(stub, limit=2, scope="tickets", page=first["next_page"])
    with pytest.raises(HostError, match="page.expired"):
        stub.pages.read(first["next_page"], repr(sorted({}.items())), "p_other")  # another principal's token


def test_activity_hides_a_ticket_that_became_invisible_between_pages(rich, stub):
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
    viewer = StubHost(env.root, env.host_state, other.ref)
    first = viewer.call("activity.list", {"scope": "tickets", "limit": 3})["data"]
    hidden = ids["Second"]
    env.tick()
    env.store.append(
        env.person_event(env.owner, hidden, "visibility.changed", visibility={"restricted": [env.owner.ref]}),
        log=hidden,
    )
    rest, page = [], first["next_page"]
    while page:
        data = viewer.call("activity.list", {"scope": "tickets", "limit": 3, "page": page})["data"]
        rest += data["items"]
        page = data["next_page"]
    assert "DEMO-0002" not in [i["key"] for i in rest]
    with pytest.raises(HostError, match="not_found"):
        viewer.call("activity.list", {"ticket": "DEMO-0002"})


def test_a_receipt_label_is_passed_through_ticket_get_and_the_log(rich, stub):
    env, ids = rich
    first = ids["First"]
    env.tick()
    env.update(
        first,
        {
            "ticket.tasks": [
                {"id": "T1", "text": "Write the export", "verify": {"cmd": "make test"}, "proves": ["AC1"]},
                {"id": "T2", "text": "Document it", "verify": None, "proves": []},
            ]
        },
    )
    env.tick()
    env.store.append({"type": "claim.taken", "actor": env.agent}, log=first)
    env.tick()
    receipt = {"cmd": "make test", "exit": 0, "ms": 5, "repo": None, "commit": None, "attested_by": "agent"}
    env.store.append({"type": "task.done", "actor": env.agent, "task": "T1", "receipt": receipt}, log=first)
    got = stub.call("ticket.get", {"ref": "DEMO-0001"})["data"]
    assert got["tasks"][0]["receipt"]["attested_by"] == "agent"
    item = next(i for i in got["log"] if i["type"] == "task.done")
    assert item["data"]["receipt"]["attested_by"] == "agent"


def test_the_parent_fallback_is_logged_never_silent(rich, stub, monkeypatch, caplog):
    """#546: no common ancestor with the default branch falls back to the parent commit with one warning line (the
    error code only, no repo content), so the fallback is never silent. A merge base the reader cannot establish is
    "diff unavailable" instead (test_a_diff_the_reader_cannot_produce_is_unavailable_never_empty). Since #565 the read
    is orch's reader in-process (no git process)."""
    import logging

    from orch.gitread.repo import Repo

    with caplog.at_level(logging.WARNING, logger="orch_dashboard.devhost"):
        normal = stub.call("diff", {"ref": "DEMO-0001"})["data"]
    assert not [r for r in caplog.records if "merge base unavailable" in r.getMessage()]  # the real merge base
    caplog.clear()
    monkeypatch.setattr(Repo, "merge_base", lambda self, a, b: None)  # no common ancestor
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.devhost"):
        data = stub.call("diff", {"ref": "DEMO-0001"})["data"]
    lines = [r for r in caplog.records if "merge base unavailable" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert lines[0].getMessage() == "merge base unavailable, using the parent commit (git.no_merge_base)"
    assert data["sources"][0]["base_sha"] != normal["sources"][0]["head_sha"]


@pytest.mark.parametrize("where", ["merge_base", "commit", "blob", "tree", "refs"])
def test_a_diff_the_reader_cannot_produce_is_unavailable_never_empty(rich, stub, monkeypatch, caplog, where):
    """#561 R7-M1 and Opus R8-L2, in orch's reader (#565): whatever part of the read cannot be established (the merge
    base, a commit, a blob, a tree, the ref store) is "diff unavailable" (observe.unavailable), never counts with an
    empty patch, "no changes" or a diff against another base."""
    import logging

    from orch.gitread.errors import Unavailable
    from orch.gitread.repo import Repo

    stub.call("diff", {"ref": "DEMO-0001"})  # positive control: it reads

    def refuse(*a, **k):
        raise Unavailable(f"cannot read the {where}")

    monkeypatch.setattr(Repo, where, property(refuse) if where == "refs" else refuse)
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.devhost"):
        with pytest.raises(HostError, match="observe.unavailable") as e:
            stub.call("diff", {"ref": "DEMO-0001"})
    assert "diff unavailable" in e.value.error["message"]
    assert [r.getMessage() for r in caplog.records if "diff unavailable" in r.getMessage()]
