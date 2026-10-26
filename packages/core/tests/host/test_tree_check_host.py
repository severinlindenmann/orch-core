"""H10a: ``tree.check`` in tier A behind ``human.prepare`` and ``human.get`` for ``verdict`` and ``approve code`` (N3).

The workspace is built in direct mode (a repo in ``settings.repos``, a ticket linked to its branch, taken to testing),
then registered with a real host whose reader children check the working tree (no git process, F2)."""

from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace

import pytest

from orch.host.pins import AgentPins
from tests.host.dispatch_helpers import Served, err, ok
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import UID, Lab, short_dir
from tests.ops.helpers import Cli
from tests.ops.humans import _hws, make_ticket, to_testing

pytestmark = pytest.mark.slow  # a host, reader children and two tickets through their gates

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'
CODE_POLICY = {
    "approvers": ["maintainer", "owner"],
    "count": 1,
    "not": ["assignees"],
    "applies": "all",
    "independent": True,
}


def _world(tmp_path, monkeypatch, who):
    """``who``: ``"owner"`` (gives the verdict) or ``"rita"``, a maintainer who did not work on the ticket (D59: the
    code gate is independent), mapped to this UID at registration."""
    base = tmp_path.resolve()  # the reader refuses a declared path with a symlinked component (/var on macOS)
    gen = _hws(base, monkeypatch, live=False)
    hws = next(gen)
    agent, me = Cli(hws), Cli(hws, grant=False, session=None)
    repo = hws.repo()
    hws.store.append(
        hws.person_event(hws.owner, "workspace", "policy.changed", gates={"code": CODE_POLICY}), log="workspace"
    )
    rita = hws.add_member("rita", "maintainer")
    key = make_ticket(agent, "A linked ticket")
    assert agent("set", key, LINKS).code == 0
    to_testing(agent, me, hws.tmp, key)
    if who == "rita":
        r = me("verdict", "pass", "--ref", key)
        assert r.code == 0, r.err
    hws.store.close()
    hws.store = None
    person = rita if who == "rita" else hws.owner
    lab = Lab.__new__(Lab)
    lab.ws, lab.tmp, lab.state = hws, base, hws.host_state
    lab.short = short_dir()
    lab.run = lab.short / "r"
    lab.pins_dir = base / "pins"
    lab.pins_dir.mkdir()
    lab.pins = AgentPins(lab.pins_dir, owner=UID)
    lab.peers = lambda person_ref=None, uid=UID: {str(uid): {"class": "person", "person": person.ref}}
    try:
        served = Served(lab)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        yield SimpleNamespace(served=served, human=Human(served, person), repo=repo, key=key, hws=hws, who=who)
    finally:
        served.close()
        lab.close()
        for _ in gen:
            pass


@pytest.fixture(params=["verdict", "approve code"])
def world(request, tmp_path, monkeypatch):
    yield from _world(tmp_path, monkeypatch, "owner" if request.param == "verdict" else "rita")


@pytest.fixture
def owner_world(tmp_path, monkeypatch):
    yield from _world(tmp_path, monkeypatch, "owner")


def decision(w):
    if w.who == "owner":
        return ("verdict", {"ref": w.key, "outcome": "pass"})
    return ("approve", {"ref": w.key, "gate": "code"})


def error(res):
    """The refusal of a result: ``{code, message, ...}``."""
    err(res)
    return res["error"]


def state(w, request_id):
    path = w.served.lab.state / "hosts" / w.served.wid / "requests" / request_id
    return json.loads(path.read_text())["state"]


def test_a_clean_tree_allows_the_decision_and_the_result_carries_the_checks(world):
    name, args = decision(world)
    prepared = world.human.prepare(name, args)
    (check,) = prepared["checks"]
    assert check["repo"] == "proj" and check["clean"] is True and check["attested_by"] == "worker"
    assert check["at"].endswith("Z")
    assert "checks" not in prepared["material"]  # not signed
    signed = world.human.sign(prepared)
    ok(world.human.submit_res(prepared["request"]["id"], signed.events))


@pytest.mark.parametrize("dirt", ["modified", "untracked", "staged"])
def test_a_dirty_tree_blocks_the_decision_at_prepare(world, dirt):
    name, args = decision(world)
    if dirt == "modified":
        (world.repo / "impl.txt").write_text("fixed but not committed\n")
    elif dirt == "untracked":
        (world.repo / "notes.txt").write_text("new\n")
    else:
        (world.repo / "impl.txt").write_text("staged\n")
        world.hws.git("add", "impl.txt")
        (world.repo / "impl.txt").write_text("broken\n")  # the worktree is back to HEAD; the index is not
    res = world.human.prepare_res(name, args)
    assert error(res)["code"] == "observe.unavailable"
    assert "uncommitted" in error(res)["message"]


def test_a_tree_dirtied_after_prepare_is_refused_at_binding(world):
    name, args = decision(world)
    h = world.human
    prepared = h.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    assert prepared["checks"][0]["clean"] is True
    (world.repo / "evil‮txt.sh").write_text("x\n")
    res = h.c.req("human.get", {"request_id": rid, "signer": h.signer})
    e = error(res)
    assert e["code"] == "observe.unavailable" and "changed since prepare" in e["message"]
    assert "evil⟨U+202E⟩txt.sh" in e["message"] and "‮" not in e["message"]  # escaped (N3)
    assert state(world, rid) == "prepared"  # nothing was bound
    (world.repo / "evil‮txt.sh").unlink()
    world.served.lab.ws.clock[0] += 5
    bound = ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))["data"]
    assert bound["request"]["state"] == "bound" and bound["checks"][0]["at"] > prepared["checks"][0]["at"]
    # bound: a later get with the same signer runs it again (once the 5 s re-check bound has passed)
    world.served.lab.ws.clock[0] += 6
    (world.repo / "impl.txt").write_text("late edit\n")
    assert error(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))["code"] == "observe.unavailable"
    (world.repo / "impl.txt").write_text("broken\n")
    again = ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))["data"]
    signed = h.sign(again)
    ok(h.submit_res(rid, signed.events))


def test_unknown_fails_closed_at_prepare_and_at_binding(owner_world, monkeypatch):
    world = owner_world
    h = world.human
    name, args = decision(world)
    prepared = h.prepare(name, args, handoff=True)
    reader = world.served.service.capabilities().git.reader
    monkeypatch.setattr(reader, "tree_checks", False)  # tier B without its worker: no answer
    e = error(h.c.req("human.get", {"request_id": prepared["request"]["id"], "signer": h.signer}))
    assert e["code"] == "observe.unavailable" and "cannot be checked" in e["message"]
    assert error(h.prepare_res(name, args))["code"] == "observe.unavailable"
    monkeypatch.setattr(reader, "tree_checks", True)
    os.mkfifo(world.repo / "impl.txt.fifo")  # a FIFO never blocks the check: it is untracked here
    assert error(h.prepare_res(name, args))["code"] == "observe.unavailable"


def test_a_booby_trapped_repo_runs_nothing_through_a_whole_decision(owner_world):
    """F2 behind the host: filter.*.clean, core.fsmonitor and core.hooksPath set in the agent-writable repo; the
    verdict goes through prepare, binding and submit and none of them ever ran."""
    world = owner_world
    hws, repo = world.hws, world.repo
    marks = [repo.parent / f"ran-{k}" for k in ("clean", "fsmonitor", "hook")]
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "reference-transaction", "pre-commit"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marks[2]}\n")
        (hooks / hook).chmod(0o755)
    (repo.parent / "fsm.sh").write_text(f"#!/bin/sh\ntouch {marks[1]}\n")
    (repo.parent / "fsm.sh").chmod(0o755)
    hws.git("config", "filter.trap.clean", f"sh -c 'touch {marks[0]}; cat'")
    hws.git("config", "core.fsmonitor", str(repo.parent / "fsm.sh"))
    hws.git("config", "core.hooksPath", str(hooks))
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=trap\n")
    os.utime(repo / "impl.txt", (3, 3))  # forces the raw-bytes hash where a filter would have run
    h = world.human
    prepared = h.prepare(*decision(world), handoff=True)
    bound = ok(h.c.req("human.get", {"request_id": prepared["request"]["id"], "signer": h.signer}))["data"]
    ok(h.submit_res(bound["request"]["id"], h.sign(bound).events))
    assert not [m.name for m in marks if m.exists()]


@pytest.mark.parametrize("tamper", ["dropped", "other repo", "not clean"])
def test_binding_requires_a_check_for_every_bound_repo_never_a_cached_one(world, tamper):
    """The re-run at binding reads the working tree now and needs a clean check for every repo in the source list;
    a request whose stored checks lost a repo or name another is refused, never bound; a stored "not clean" is not
    trusted either way: the current tree decides."""
    h = world.human
    name, args = decision(world)
    prepared = h.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    path = world.served.lab.state / "hosts" / world.served.wid / "requests" / rid
    rec = json.loads(path.read_text())
    if tamper == "dropped":
        rec["prepared"]["checks"] = []
    elif tamper == "other repo":
        rec["prepared"]["checks"][0]["repo"] = "nope"
    else:
        rec["prepared"]["checks"][0]["clean"] = False
    path.write_text(json.dumps(rec))
    res = h.c.req("human.get", {"request_id": rid, "signer": h.signer})
    if tamper == "not clean":  # the stored answer is never used: the tree is read now, found clean, and bound
        assert ok(res)["data"]["checks"][0]["clean"] is True
        return
    assert error(res)["code"] == "observe.unavailable"
    assert state(world, rid) == "prepared"


@pytest.mark.parametrize("move", ["other commit", "same commit, other branch"])
def test_clean_is_tied_to_the_signed_commit_at_binding(world, move):
    """Opus #413 finding 1: prepare at C on feat/x, switch to a clean branch (at D, or at C under another name), bind:
    refused, although the tree is clean there."""
    h = world.human
    name, args = decision(world)
    prepared = h.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    world.hws.git("checkout", "-q", "-b", "other")
    if move == "other commit":
        (world.repo / "impl.txt").write_text("D\n")
        world.hws.git("commit", "-qam", "D")
    assert world.hws.git("status", "--porcelain") == ""  # clean there
    e = error(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))
    assert e["code"] == "observe.unavailable" and "not at the commit" in e["message"]
    assert state(world, rid) == "prepared"
    world.hws.git("checkout", "-q", "feat/x")
    assert ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))["data"]["request"]["state"] == "bound"


def test_a_dry_run_runs_no_tree_check(owner_world, monkeypatch):
    """Section 0.8.1: a dry run stores nothing and runs no tree.check job, so it carries no checks."""
    world = owner_world
    reader = world.served.service.capabilities().git.reader

    def boom(*a, **kw):
        raise AssertionError("a dry run ran tree.check")

    monkeypatch.setattr(reader, "tree_check", boom)
    (world.repo / "impl.txt").write_text("dirty, but a dry run does not look\n")
    name, args = decision(world)
    data = ok(world.human.prepare_res(name, args, dry_run=True))["data"]
    assert data["request"] is None and "checks" not in data


def test_a_bound_request_polled_within_5_seconds_is_not_walked_again(owner_world, monkeypatch):
    world = owner_world
    h = world.human
    prepared = h.prepare(*decision(world), handoff=True)
    rid = prepared["request"]["id"]
    ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))  # binds: always checked
    reader = world.served.service.capabilities().git.reader
    calls = []
    real = reader.tree_check
    monkeypatch.setattr(reader, "tree_check", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    for _ in range(3):
        ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))
    assert calls == []
    world.served.lab.ws.clock[0] += 6
    ok(h.c.req("human.get", {"request_id": rid, "signer": h.signer}))
    assert calls == [1]


def test_pins_are_taken_at_open_under_the_workspace_principal_and_never_replaced(owner_world):
    world = owner_world
    service = world.served.service
    pin_file = world.served.lab.state / "hosts" / world.served.wid / "git-pins.json"
    pins = json.loads(pin_file.read_text())["repos"]  # pinned at open, before any op read git
    assert list(pins) == [str(world.repo)]
    reader = service.capabilities().git.reader
    assert reader.principal == f"host:{world.served.wid}"
    before = reader.pins.get(str(world.repo))
    service._git_declared = None  # as if settings changed: sync again
    service.submit(service.sync_git).result()
    assert reader.pins.get(str(world.repo)) is before


def test_a_failed_pin_is_retried_after_a_backoff_not_on_every_read(owner_world, monkeypatch):
    world = owner_world
    service = world.served.service
    reader = service.capabilities().git.reader
    calls = []
    monkeypatch.setattr(reader, "sync", lambda declared: calls.append(1) or {p: "cannot pin" for p in declared})
    service._git_declared = None
    for _ in range(5):
        service.submit(service.sync_git).result()
    assert calls == [1] and service._git_backoff == 60.0
    service._git_retry_at = 0.0  # the backoff has run out
    service.submit(service.sync_git).result()
    assert calls == [1, 1] and service._git_backoff == 120.0


def test_an_unreadable_pin_file_denies_git(owner_world):
    """A pin file that cannot be read is never replaced by fresh pins: git is denied, so decisions fail closed."""
    world = owner_world
    service = world.served.service
    pin_file = world.served.lab.state / "hosts" / world.served.wid / "git-pins.json"
    pin_file.write_text("not json")

    def rebuild():
        service.caps = None
        return service.capabilities().git.enabled

    assert service.submit(rebuild).result() is False
    assert error(world.human.prepare_res(*decision(world)))["code"] == "observe.unavailable"
