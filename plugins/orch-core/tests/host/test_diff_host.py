"""``diff`` on the workspace socket (§0.8 v1.4, §0.5; the H3b1 follow-up): served through the host's reader children
under the caller's principal, with the ticket's visibility and its linked repos checked first.

The workspace has two declared repos, ``proj`` (linked to the tickets, branch ``feat/x`` one commit past ``main``) and
``other`` (declared, never linked), and is registered with a real host (reader pool, pins in the state dir)."""

from __future__ import annotations

import base64
import errno
import json
import threading
import time

import pytest

from orch.gitread.errors import Busy, Unavailable
from orch.host.dispatch import served_ops
from orch.ops.caps import Capabilities
from orch.ops.diff_core import diff_result
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.live_helpers import World
from tests.host.test_dispatch_matrix import as_agent_class

pytestmark = pytest.mark.slow  # a host and git reader children

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


@pytest.fixture
def world(tmp_path):
    lab = Lab(tmp_path.resolve())  # the reader refuses a declared path with a symlinked component (/var on macOS)
    ws = lab.ws
    ws.store = ws.other()
    try:
        other = ws.repo("other")
        proj = ws.repo("proj")
        ws.git("branch", "main", "HEAD")
        (proj / "impl.txt").write_text("fixed\n")
        ws.git("commit", "-qam", "fix the \x1b[31mtables\u202e")  # a subject with a control and a bidi character
        ws.tick()
        repos = {"proj": {"path": str(proj)}, "other": {"path": str(other)}}
        event = ws.person_event(ws.owner, "workspace", "settings.changed", set={"repos": repos})
        ws.store.append(event, log="workspace")
    finally:
        ws.store.close()
        ws.store = None
    try:
        served = World(lab)  # a Served workspace that can issue the owner further grants (more principals)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        c = served.client()
        tickets = {}
        for name in ("open", "secret"):
            uid, key = served.ticket(f"the {name} ticket")
            ok(c.req("set", {"ref": key, "pairs": [LINKS]}, grant=served.grant, session=SESSION, idem=True))
            tickets[name] = (uid, key)
        served.restrict(tickets["secret"][0], [lab.owner])
        yield served, proj, tickets
    finally:
        served.close()
        lab.close()


def diff(served, args, *, grant=None):
    return served.client().req("diff", args, grant=grant, session=SESSION if grant else None)


def direct(served, uid, **kw):
    """Direct mode's ``diff`` core on the same workspace (the in-process reader)."""
    store = served.lab.ws.other()
    try:
        return diff_result(Capabilities.direct({}), store, store.ticket(uid), **kw)
    finally:
        store.close()


def pinned(data):
    """The host pins the first merge base it sees (H6c): later diffs say ``host_pinned`` where direct mode, which keeps
    no host state, says ``local_ref``. Everything else is equal."""
    return {
        **data,
        "sources": [
            {**s, "base_source": "local_ref" if s["base_source"] == "host_pinned" else s["base_source"]}
            for s in data["sources"]
        ],
    }


def test_diff_is_served_and_listed(world):
    served, _, _ = world
    welcome = served.client().welcome
    assert welcome["ops"]["diff"] == 1 and "diff/1" in welcome["caps"]
    assert "diff" in served_ops()


def test_a_visible_linked_diff_equals_direct_mode(world):
    served, proj, tickets = world
    uid, key = tickets["open"]
    got = ok(diff(served, {"ref": key}))["data"]
    assert got == direct(served, uid)  # the first diff: nothing is pinned yet
    assert set(got) == {"sources", "files", "unified", "truncated"}  # no field added
    (src,) = got["sources"]
    assert src["repo"] == "proj" and src["branch"] == "feat/x" and src["base_source"] == "local_ref"
    assert set(src) == {
        "repo", "branch", "base_sha", "head_sha", "base_source", "base_moved", "commits", "commits_more"
    }  # fmt: skip
    (commit,) = src["commits"]
    assert "\x1b" not in commit["subject"] and "\u202e" not in commit["subject"]  # untrusted text, cleaned
    assert got["files"] == [
        {"repo": "proj", "path": "impl.txt", "old_path": None, "status": "M", "add": 1, "del": 1, "binary": False,
         "too_large": False}
    ]  # fmt: skip
    assert "-broken\n+fixed\n" in got["unified"] and got["truncated"] is False
    # repo, context and max_bytes as direct mode takes them
    for kw in ({"repo": "proj"}, {"context": 0}, {"max_bytes": 1024}):
        assert pinned(ok(diff(served, {"ref": key, **kw}))["data"]) == direct(served, uid, **kw), kw


def test_the_dashboard_adapter_reads_the_served_shape(world):
    """``addons/dashboard``'s Changes tab calls ``diff {ref, context, repo?}`` and hands the result to its
    ``adapters.changes``: the real adapter, loaded from the addon's source, reads the served result."""
    import importlib.util
    from pathlib import Path

    src = Path(__file__).resolve().parents[4] / "addons/dashboard/server/src/orch_dashboard/adapters/changes.py"
    spec = importlib.util.spec_from_file_location("dashboard_changes", src)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    served, _, tickets = world
    key = tickets["open"][1]
    data = ok(diff(served, {"ref": key, "context": 3, "repo": "proj"}))["data"]
    got = adapter.changes(data, key)
    (src_,) = data["sources"]
    assert (got["branch"], got["base"], got["head"]) == ("feat/x", src_["base_sha"], src_["head_sha"])
    assert got["sources"] == [{"repo": "proj", "branch": "feat/x", "base": src_["base_sha"], "head": src_["head_sha"]}]
    assert (got["additions"], got["deletions"], got["truncated"]) == (1, 1, False)
    (f,) = got["files"]
    assert f["path"] == "impl.txt" and f["status"] == "M" and f["lines"].startswith("@@ ")
    assert "-broken\n+fixed" in f["lines"]


def _norm(res, ref):
    return json.dumps({k: v for k, v in res.items() if k != "id"}).replace(ref, "K")


@pytest.mark.parametrize("column", ["G", "N"])
def test_a_restricted_ticket_is_not_found_exactly_like_a_missing_one(world, column):
    served, _, tickets = world
    key = tickets["secret"][1]
    assert ok(diff(served, {"ref": key}))["data"]["sources"]  # the owner (P, on the list) sees it
    grant = None
    if column == "N":
        as_agent_class(served)  # an agent-class peer without a grant: no-person
    else:  # the grant's person is the owner: drop them from the list
        served.restrict(tickets["secret"][0], [served.quinn.ref])
        grant = served.grant
    hidden = diff(served, {"ref": key}, grant=grant)
    missing = diff(served, {"ref": "DEMO-9999"}, grant=grant)
    assert err(hidden) == "not_found"
    assert _norm(hidden, key) == _norm(missing, "DEMO-9999")
    assert "secret" not in json.dumps(hidden)


def test_a_repo_not_linked_to_the_ticket_is_not_found(world):
    served, _, tickets = world
    key = tickets["open"][1]
    declared = diff(served, {"ref": key, "repo": "other"})  # in settings.repos, not in the ticket's links
    undeclared = diff(served, {"ref": key, "repo": "nope"})
    assert err(declared) == "not_found" and err(undeclared) == "not_found"
    assert _norm(declared, "other") == _norm(undeclared, "nope")


def test_column_n_reads_a_workspace_ticket_and_g_reads_too(world):
    """§0.5: ``diff`` is open to P, G and N; N (no-person) sees ``workspace`` tickets only."""
    served, _, tickets = world
    uid, key = tickets["open"]
    want = direct(served, uid)
    assert ok(diff(served, {"ref": key}, grant=served.grant))["data"] == want  # G
    as_agent_class(served)
    assert pinned(ok(diff(served, {"ref": key}))["data"]) == want  # N (the first diff pinned the base)


def test_the_arguments(world):
    served, _, tickets = world
    key = tickets["open"][1]
    for bad in ({"ref": key, "context": 11}, {"ref": key, "max_bytes": 10}, {"ref": key, "since": "latest"},
                {"ref": key, "head": "0" * 40},  # no repo
                {"ref": key, "repo": "proj", "head": "0" * 39}, {"ref": key, "repo": "proj", "base": "G" * 40},
                {"ref": key, "repo": "proj", "since": "my_approval", "head": "0" * 40},
                {"ref": key, "repo": "proj", "since": "my_approval", "base": "0" * 40}, {}):  # fmt: skip
        assert err(diff(served, bad)) == "invalid.input", bad
    c = served.client()
    assert err(c.req("diff", {"ref": key}, dry_run=True)) == "invalid.input"
    assert err(c.req("diff", {"ref": key}, files={"ref": {"b64": base64.b64encode(b"x").decode()}})) == "blob.invalid"


def test_runs_under_the_callers_principal(world, monkeypatch):
    served, _, tickets = world
    reader = served.service.capabilities().git.reader
    seen = []
    real = type(reader).diff

    def spy(self, *a, **k):
        seen.append(k["principal"])
        return real(self, *a, **k)

    monkeypatch.setattr(type(reader), "diff", spy)
    ok(diff(served, {"ref": tickets["open"][1]}))
    ok(diff(served, {"ref": tickets["open"][1]}, grant=served.grant))
    assert len(seen) == 2 and seen[0].startswith("uid:") and seen[1].startswith("g:")


def test_quota_host_under_a_burst(world):
    """The pool's queue bound (§0.11) answers ``quota.host``, retryable: a burst of this principal's git reads holds the
    one child a principal may have and fills its queue, so the next ``diff`` is refused at once, never queued."""
    served, _, tickets = world
    pool = served.service.git_pool
    principal = "uid:" + next(iter(served.lab.peers()))
    release, running = threading.Event(), threading.Event()
    real = pool._run

    def run(hello, req, deadline=None):  # the burst's reads stay in their child until released; diff runs for real
        if req.get("op") != "burst":
            return real(hello, req, deadline)
        running.set()
        release.wait(20)
        return {"ok": {}}

    pool._run = run
    burst = [
        threading.Thread(target=pool.call, args=(principal, {}, {"op": "burst"}))
        for _ in range(pool._queue_per_principal)
    ]
    try:
        for t in burst:
            t.start()
        assert running.wait(10)
        while pool._principals.get(principal, [None, 0])[1] < pool._queue_per_principal:
            time.sleep(0.01)
        res = diff(served, {"ref": tickets["open"][1]})
        assert err(res) == "quota.host" and res["error"]["retryable"] is True
    finally:
        release.set()
        for t in burst:
            t.join(30)
        pool._run = real
    ok(diff(served, {"ref": tickets["open"][1]}))  # served again once the burst is over


@pytest.mark.parametrize(
    ("raised", "code"),
    [(Unavailable("the reader child died"), "observe.unavailable"), (Busy("queue full"), "quota.host")],
)
def test_reader_failures_map_to_their_codes(world, monkeypatch, raised, code):
    served, _, tickets = world
    reader = served.service.capabilities().git.reader

    def fail(self, *a, **k):
        raise raised

    monkeypatch.setattr(type(reader), "diff", fail)
    res = diff(served, {"ref": tickets["open"][1]})
    assert err(res) == code


def test_a_declared_remote_that_differs_is_repo_remote_mismatch(world):
    served, proj, tickets = world
    ws = served.lab.ws
    ws.git("remote", "add", "origin", "https://github.com/evil/proj.git")
    ws.tick()

    def declare(store):
        repos = {"proj": {"path": str(proj), "remote": "https://github.com/acme/proj"}}
        store.append(ws.person_event(ws.owner, "workspace", "settings.changed", set={"repos": repos}), log="workspace")

    served.append(declare)
    res = diff(served, {"ref": tickets["open"][1]})
    assert err(res) == "repo.remote_mismatch" and res["exit"] == 3


@pytest.mark.parametrize("column", ["P", "G", "N"])
def test_a_reader_failure_names_no_host_path_except_to_a_manager(world, column):
    """§0.5/§0.8.5: a repo's path is shown only to a person-class owner or maintainer. The reader's reason names the
    missing ``.git`` by its absolute path: P (the owner) gets it, G and N get ``repo proj: not readable``."""
    import shutil

    served, proj, tickets = world
    shutil.rmtree(proj / ".git")
    grant = served.grant if column == "G" else None
    if column == "N":
        as_agent_class(served)
    res = diff(served, {"ref": tickets["open"][1]}, grant=grant)
    assert err(res) == "observe.unavailable"
    message = res["error"]["message"]
    if column == "P":
        assert str(proj) in message
    else:
        assert message == "repo proj: not readable"
        assert str(served.lab.tmp) not in json.dumps(res) and "/" not in message


def slow_reader(served, monkeypatch, seconds):
    """Every reader ``diff`` takes ``seconds`` before it reads (in the reader phase, off the worker thread)."""
    reader = served.service.capabilities().git.reader
    real = type(reader).diff
    started = threading.Event()

    def slow(self, *a, **k):
        started.set()
        time.sleep(seconds)
        return real(self, *a, **k)

    monkeypatch.setattr(type(reader), "diff", slow)
    return started


def test_a_slow_diff_does_not_hold_the_workspace(world, monkeypatch):
    """The reader phase runs off the workspace's one worker thread: while a G caller's diff waits 6 s on its
    reader, the owner's ``ticket.get`` answers within 2 s, the owner's own diff is not refused, and a second diff of
    the same principal is ``quota.host`` (one in flight per principal)."""
    served, _, tickets = world
    key = tickets["open"][1]
    started = slow_reader(served, monkeypatch, 6.0)
    out = {}
    slow = threading.Thread(target=lambda: out.setdefault("g", diff(served, {"ref": key}, grant=served.grant)))
    slow.start()
    try:
        assert started.wait(10)
        t0 = time.monotonic()
        ok(served.client().req("ticket.get", {"ref": key}))
        assert time.monotonic() - t0 < 2.0
        second = diff(served, {"ref": key}, grant=served.grant)
        assert err(second) == "quota.host" and second["error"]["retryable"] is True
        assert ok(diff(served, {"ref": key}))["data"]["sources"]  # another principal (the owner) is not refused
    finally:
        slow.join(30)
    assert pinned(ok(out["g"])["data"]) == direct(served, tickets["open"][0])
    monkeypatch.undo()
    ok(diff(served, {"ref": key}, grant=served.grant))  # the slot is free again


def test_diffs_hold_at_most_two_reader_children_host_wide(world, monkeypatch):
    """Two of the pool's four children stay free for everything else: with diffs of four principals held slow in
    their reader children, two run and the other two are ``quota.host`` at once (retryable), and the owner's ``show``
    (which observes the linked repo through a reader child) stays fast."""
    from orch.host import diff as host_diff

    served, _, tickets = world
    key = tickets["open"][1]
    assert host_diff.MAX_DIFFS == 2
    pool = served.service.git_pool
    real, release, running = pool._run, threading.Event(), threading.Semaphore(0)

    def run(hello, req, deadline=None):
        if req.get("op") == "diff":
            running.release()
            release.wait(20)
        return real(hello, req, deadline)

    monkeypatch.setattr(pool, "_run", run)
    grants = [served.grant] + [grant_value(served.owner_grant()) for _ in range(2)]
    out = {}
    held = [
        threading.Thread(target=lambda g=g: out.setdefault(g, diff(served, {"ref": key}, grant=g))) for g in grants[:2]
    ]
    try:
        for t in held:
            t.start()
        assert running.acquire(timeout=10) and running.acquire(timeout=10)  # two diffs are in their children
        for grant in (grants[2], None):  # a third grant, and the owner (P): two more principals
            res = diff(served, {"ref": key}, grant=grant)
            assert err(res) == "quota.host" and res["error"]["retryable"] is True
        t0 = time.monotonic()
        ok(served.client().req("show", {"ref": key}))
        assert time.monotonic() - t0 < 2.0
        assert pool.peak <= 3  # the two diffs and show's observe
    finally:
        release.set()
        for t in held:
            t.join(30)
    assert all(ok(out[g])["data"]["sources"] for g in grants[:2])
    ok(diff(served, {"ref": key}, grant=grants[2]))  # served once the others are done


def test_a_ticket_restricted_while_its_diff_runs_is_not_found(world, monkeypatch):
    """Visibility is checked again on the worker before the answer: restricted during the reader phase, the diff is
    ``not_found``, byte-identical to a ticket that does not exist."""
    served, _, tickets = world
    uid, key = tickets["open"]
    started = slow_reader(served, monkeypatch, 2.0)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", diff(served, {"ref": key}, grant=served.grant)))
    t.start()
    try:
        assert started.wait(10)
        served.restrict(uid, [served.quinn.ref])  # the grant's person (the owner) is not on the list
    finally:
        t.join(30)
    monkeypatch.undo()
    missing = diff(served, {"ref": "DEMO-9999"}, grant=served.grant)
    assert err(out["res"]) == "not_found"
    assert _norm(out["res"], key) == _norm(missing, "DEMO-9999")


def test_a_ticket_restricted_while_its_diff_fails_is_not_found(world, monkeypatch):
    """The re-check also guards the error path: restricted during a reader phase that then fails
    (``observe.unavailable``), the answer is ``not_found``, not the reader's error."""
    served, _, tickets = world
    uid, key = tickets["open"]
    reader = served.service.capabilities().git.reader
    started = threading.Event()

    def fail(self, *a, **k):
        started.set()
        time.sleep(2.0)
        raise Unavailable("the reader child died")

    monkeypatch.setattr(type(reader), "diff", fail)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", diff(served, {"ref": key}, grant=served.grant)))
    t.start()
    try:
        assert started.wait(10)
        served.restrict(uid, [served.quinn.ref])
    finally:
        t.join(30)
    monkeypatch.undo()
    missing = diff(served, {"ref": "DEMO-9999"}, grant=served.grant)
    assert err(out["res"]) == "not_found"
    assert _norm(out["res"], key) == _norm(missing, "DEMO-9999")


def test_one_request_has_one_budget_for_all_its_repos(world, monkeypatch):
    """The reader call gets the request's remaining budget as its deadline (not the pool's own 30 s), and a
    request over budget is ``observe.unavailable`` without its reason (G is not a manager)."""
    from orch.host import diff as host_diff

    served, _, tickets = world
    monkeypatch.setattr(host_diff, "BUDGET_S", 1.0)
    pool = served.service.git_pool
    walls = []

    def run(hello, req, deadline=None):
        walls.append(deadline - time.monotonic())
        time.sleep(max(0.0, deadline - time.monotonic()))
        return {"error": {"code": "observe.unavailable", "message": "the reader child ran past its deadline"}}

    monkeypatch.setattr(pool, "_run", run)
    t0 = time.monotonic()
    res = diff(served, {"ref": tickets["open"][1]}, grant=served.grant)
    assert err(res) == "observe.unavailable" and res["error"]["message"] == "repo proj: not readable"
    assert time.monotonic() - t0 < 5.0
    assert len(walls) == 1 and 0 < walls[0] <= 1.0


def test_an_unpinnable_or_missing_working_copy_is_observe_unavailable(world):
    served, proj, tickets = world
    import shutil

    shutil.rmtree(proj / ".git")
    assert err(diff(served, {"ref": tickets["open"][1]})) == "observe.unavailable"


def grant_value(gid):
    """The ``auth.grant`` value of a grant ``World.owner_grant`` issued."""
    from orch import crypto
    from tests.store.helpers import GRANT_SECRET

    return f"{gid}.{crypto.b64u(GRANT_SECRET)}"


# --- H6c: since, head, base, host_pinned ---------------------------------------------------------------------------

H6C = [
    {"since": "my_approval"},
    {"repo": "proj", "head": "a" * 40},
    {"repo": "proj", "base": "b" * 40},
]


@pytest.mark.parametrize("extra", H6C, ids=["since", "head", "base"])
def test_the_h6c_arguments_on_a_restricted_ticket_are_byte_identical_to_a_missing_one(world, extra):
    served, _, tickets = world
    uid, key = tickets["secret"]
    served.restrict(uid, [served.quinn.ref])  # the grant's person (the owner) is off the list
    hidden = diff(served, {"ref": key, **extra}, grant=served.grant)
    missing = diff(served, {"ref": "DEMO-9999", **extra}, grant=served.grant)
    assert err(hidden) == "not_found" and _norm(hidden, key) == _norm(missing, "DEMO-9999")
    as_agent_class(served)  # no-person
    assert err(diff(served, {"ref": key, **extra})) == "not_found"


def test_since_without_an_approval_and_head_base_outside_the_ranges_are_not_found(world):
    served, proj, tickets = world
    uid, key = tickets["open"]
    res = diff(served, {"ref": key, "since": "my_approval"})
    assert err(res) == "not_found" and "no approval of yours" in res["error"]["message"]
    # a commit that exists but is no part of the ticket's history, and one that does not exist: one answer
    outside = served.lab.ws.git("rev-parse", "main")
    texts = set()
    for sha in (outside, "e" * 40):
        res = diff(served, {"ref": key, "repo": "proj", "head": sha})
        assert err(res) == "not_found"
        texts.add(res["error"]["message"])
    assert len(texts) == 1
    res = diff(served, {"ref": key, "repo": "proj", "base": "e" * 40})
    assert err(res) == "not_found" and res["error"]["message"] in texts
    # the observed head and its default base are always allowed, and say so
    head = served.lab.ws.git("rev-parse", "feat/x")
    got = ok(diff(served, {"ref": key, "repo": "proj", "head": head, "base": outside}))["data"]
    assert got["sources"][0]["base_sha"] == outside


def test_the_first_merge_base_is_pinned_and_main_moving_is_counted(world):
    served, proj, tickets = world
    uid, key = tickets["open"]
    git = served.lab.ws.git
    first = ok(diff(served, {"ref": key}))["data"]["sources"][0]
    assert first["base_source"] == "local_ref"
    base = first["base_sha"]
    path = served.lab.state / "hosts" / served.wid / "diff-bases.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text()) == {uid: {"local:proj": base}}
    again = ok(diff(served, {"ref": key}))["data"]["sources"][0]
    assert (again["base_source"], again["base_sha"], again["base_moved"]) == ("host_pinned", base, 0)
    # main moves on and the branch merges it: the pin stays, and the commits it hides are counted
    git("checkout", "-q", "main")
    (proj / "main.txt").write_text("main\n")
    git("add", "main.txt")
    git("commit", "-qm", "main moves")
    git("checkout", "-q", "feat/x")
    git("merge", "-q", "--no-edit", "main")
    moved = ok(diff(served, {"ref": key}))["data"]["sources"][0]
    assert (moved["base_source"], moved["base_sha"], moved["base_moved"]) == ("host_pinned", base, 1)
    assert json.loads(path.read_text()) == {uid: {"local:proj": base}}  # never moved forward


@pytest.mark.parametrize("extra", H6C, ids=["since", "head", "base"])
def test_the_h6c_arguments_keep_the_limits(world, monkeypatch, extra):
    """One diff in flight per principal, and the ticket's visibility checked again after the read, on success and on
    error, with the new arguments as with the old."""
    served, _, tickets = world
    uid, key = tickets["open"]
    started = slow_reader(served, monkeypatch, 3.0)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("g", diff(served, {"ref": key, **extra}, grant=served.grant)))
    t.start()
    try:
        assert started.wait(10)
        second = diff(served, {"ref": key, **extra}, grant=served.grant)
        assert err(second) == "quota.host" and second["error"]["retryable"] is True
        served.restrict(uid, [served.quinn.ref])
    finally:
        t.join(30)
    monkeypatch.undo()
    missing = diff(served, {"ref": "DEMO-9999"}, grant=served.grant)
    assert err(out["g"]) == "not_found" and _norm(out["g"], key) == _norm(missing, "DEMO-9999")  # success or error path


def test_diff_ext_is_listed(world):
    served, _, _ = world
    assert "diff_ext/1" in served.client().welcome["caps"]


def test_an_agent_cannot_pin_a_base_that_hides_its_commits(world):
    """Opus's repro: M - C1 - E(evil.txt) - N on the branch; main is moved to E, a diff pins E, main is moved back. A
    grant (G) records no pin at all, a person's pin is used only while the default branch still contains it, and the
    owner's diff shows evil.txt."""
    served, proj, tickets = world
    uid, key = tickets["open"]
    git = served.lab.ws.git
    m = git("rev-parse", "main")
    (proj / "evil.txt").write_text("evil\n")
    git("add", "evil.txt")
    git("commit", "-qm", "evil")
    evil = git("rev-parse", "HEAD")
    (proj / "n.txt").write_text("n\n")
    git("add", "n.txt")
    git("commit", "-qm", "n")
    git("branch", "-f", "main", evil)
    path = served.lab.state / "hosts" / served.wid / "diff-bases.json"
    by_grant = ok(diff(served, {"ref": key}, grant=served.grant))["data"]["sources"][0]
    assert by_grant["base_sha"] == evil and not path.exists()  # column G pins nothing
    ok(diff(served, {"ref": key}))  # the owner (column P) pins the then-current merge base: evil
    assert json.loads(path.read_text()) == {uid: {"local:proj": evil}}
    git("branch", "-f", "main", m)
    got = ok(diff(served, {"ref": key}))["data"]
    assert got["sources"][0]["base_sha"] == m and got["sources"][0]["base_source"] == "local_ref"
    assert "evil.txt" in {f["path"] for f in got["files"]} and "+evil" in got["unified"]


def test_a_wrong_length_head_is_the_same_not_found_as_any_unknown_one(world):
    served, _, tickets = world
    key = tickets["open"][1]
    a = diff(served, {"ref": key, "repo": "proj", "head": "a" * 64})  # a sha256 id in a sha1 repository
    b = diff(served, {"ref": key, "repo": "proj", "head": "a" * 40})
    assert err(a) == "not_found" and a["error"]["message"] == b["error"]["message"]


def test_two_recorders_of_different_tickets_keep_both_pins(tmp_path):
    import threading

    from orch.host.diff import BaseStore

    path = tmp_path / "diff-bases.json"
    assert BaseStore.at(path) is BaseStore.at(tmp_path / "." / "diff-bases.json")
    barrier = threading.Barrier(8)

    def rec(i):
        store = BaseStore.at(path)
        barrier.wait()
        store.record(f"t{i}", "https://github.com/acme/app", f"{i:040x}")

    threads = [threading.Thread(target=rec, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert json.loads(path.read_text()) == {f"t{i}": {"https://github.com/acme/app": f"{i:040x}"} for i in range(8)}
    assert oct(path.stat().st_mode & 0o777) == "0o600" and [p.name for p in tmp_path.iterdir()] == [path.name]
    BaseStore.at(path).record("t0", "https://github.com/ACME/App", "f" * 40)  # same identity, other case: kept as is
    assert json.loads(path.read_text())["t0"] == {"https://github.com/acme/app": f"{0:040x}"}
