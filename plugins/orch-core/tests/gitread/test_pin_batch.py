"""#471/#468: :meth:`HostGitReader.sync` pins every declared repo in **one** reader child (``pin_many``, profile
``pin``), with the per-repo confinement, refusals and fail-closed semantics of the one-child-per-repo ``pin`` it
replaces: a bad or unsafe repo is refused on its own and never poisons the others."""

from __future__ import annotations

import os
import time

import pytest

from orch.gitread import limits, ops
from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.gitread.errors import Busy, Unavailable
from orch.gitread.pinning import pin_repo


def committed(make_repo, name):
    r = make_repo(name)
    r.write("a.txt", "1\n")
    r.commit()
    return r


def reason(path, roots):
    try:
        pin_repo(path, roots)
    except Unavailable as e:
        return e.reason
    return None


@pytest.fixture
def mixed(make_repo, tmp_path):
    """Two good repos, a missing path, a ``.git`` symlink, a ``.git`` file borrowing another repo, a plain directory
    and an unclean path: ``(declared, good, bad)``."""
    base = tmp_path.resolve()
    good = [committed(make_repo, "good1"), committed(make_repo, "good2")]
    victim = committed(make_repo, "victim")
    link = base / "link"
    link.mkdir()
    os.symlink(victim.gitdir, link / ".git")
    borrow = base / "borrow"
    borrow.mkdir()
    (borrow / ".git").write_text(f"gitdir: {victim.gitdir}\n")
    plain = base / "plain"
    plain.mkdir()
    bad = [str(base / "missing"), str(link), str(borrow), str(plain), str(good[0].path) + "/."]
    declared = [str(good[0].path), *bad[:2], str(good[1].path), *bad[2:]]
    return declared, [str(g.path) for g in good], bad


def test_a_batch_pins_the_good_repos_and_refuses_each_bad_one_alone(mixed, tmp_path):
    declared, good, bad = mixed
    pool = ReaderPool()
    host = HostGitReader(pool, PinStore(tmp_path.resolve() / "git-pins.json"))
    res = host.sync(declared)
    assert pool.started == 1  # one child for the whole declared set
    assert {p: res[p] for p in good} == {p: None for p in good}
    clean = [p for p in declared if not p.endswith("/.")]
    for p in bad:
        assert res[p], p
        if not p.endswith("/."):  # the same refusal, word for word, as pinning that repo on its own
            assert res[p] == reason(p, clean), p
        assert host.pins.get(p) is None
    for p in good:
        assert host.pins.get(p) == pin_repo(p, clean)
    # the same answer as one child per repo gave (the old path, reproduced with the single-repo op)
    one = ReaderPool()
    for p in clean:
        try:
            got = one.call("x", {"profile": "pin", "roots": clean}, {"op": "pin", "args": {"path": p}})
            assert res[p] is None and host.pins.get(p).to_json() == got["pins"]
        except Unavailable as e:
            assert res[p] == e.reason


def test_forty_repos_are_pinned_in_one_child(make_repo):
    repos = [str(committed(make_repo, f"r{i:02d}").path) for i in range(40)]
    pool = ReaderPool()
    host = HostGitReader(pool, PinStore())
    t0 = time.monotonic()
    res = host.sync(repos)
    took = time.monotonic() - t0
    assert res == {p: None for p in repos}
    assert pool.started == 1
    assert took < 10.0, took  # was 40 children (~70 ms each on a laptop, several times that on a loaded runner)
    # unpinnable repos (the paging tests' /srv/<name>) cost one child too, also on every retry
    pool2 = ReaderPool()
    host2 = HostGitReader(pool2, PinStore())
    missing = [f"/nonexistent-f471/r{i:02d}" for i in range(41)]
    assert all(host2.sync(missing).values())
    assert all(host2.sync(missing[:40]).values())
    assert pool2.started == 2


def test_more_repos_than_one_batch_take_more_children(make_repo, monkeypatch):
    monkeypatch.setattr(limits, "PIN_BATCH", 3)
    repos = [str(committed(make_repo, f"r{i}").path) for i in range(7)]
    pool = ReaderPool()
    host = HostGitReader(pool, PinStore())
    assert host.sync(repos) == {p: None for p in repos}
    assert pool.started == 3


class Spy(ReaderPool):
    """A pool whose ``pin_many`` fails as a whole (``mode``), the single ``pin`` running for real."""

    def __init__(self, mode, **kw):
        super().__init__(**kw)
        self.mode = mode
        self.ops = []

    def call(self, principal, hello, req, **kw):
        self.ops.append(req["op"])
        if req["op"] == "pin_many":
            if self.mode == "busy":
                raise Busy("too many git reads are waiting; retry shortly")
            if self.mode == "dead":
                raise Unavailable("the reader took longer than 30 s")
            got = super().call(principal, hello, req, **kw)
            return self.mode(got)
        return super().call(principal, hello, req, **kw)


@pytest.mark.parametrize(
    "mode",
    [
        "dead",
        lambda got: {"results": got["results"][:-1]},  # one answer short
        lambda got: {"results": got["results"] + [{"error": {"code": "x", "message": "y"}}]},  # one too many
        lambda got: {"results": [{"pins": {"worktree": 1}} for _ in got["results"]]},  # malformed pins
        lambda got: {"results": [{"pins": r.get("pins"), "error": 1} for r in got["results"]]},  # both
        lambda got: {"results": "nope"},
        lambda got: {**got, "extra": 1},
    ],
)
def test_a_batch_that_fails_as_a_whole_falls_back_to_one_child_per_repo(mixed, mode):
    """A child that dies, overruns or answers malformed never decides a repo: every repo of that batch is pinned
    again on its own, so a repo that kills the batch child cannot poison the others."""
    declared, good, bad = mixed
    pool = Spy(mode)
    host = HostGitReader(pool, PinStore())
    res = host.sync(declared)
    clean = [p for p in declared if not p.endswith("/.")]
    assert pool.ops == ["pin_many"] + ["pin"] * len(clean)
    assert {p: res[p] for p in good} == {p: None for p in good}
    assert all(res[p] for p in bad)
    for p in good:
        assert host.pins.get(p) == pin_repo(p, clean)


def test_a_busy_batch_is_not_retried_repo_by_repo(mixed):
    declared, good, bad = mixed
    pool = Spy("busy")
    host = HostGitReader(pool, PinStore())
    res = host.sync(declared)
    assert pool.ops == ["pin_many"]
    assert all(res[p] for p in declared) and host.pins.paths() == []


# ---------------------------------------------------------------- the child's side of pin_many


def test_pin_many_answers_per_path_in_order(mixed):
    declared, good, bad = mixed
    clean = [p for p in declared if not p.endswith("/.")]
    got = ops.run("pin", {"profile": "pin", "roots": clean}, {"op": "pin_many", "args": {"paths": clean}})
    assert set(got) == {"results"} and len(got["results"]) == len(clean)
    for p, r in zip(clean, got["results"], strict=True):
        if p in good:
            assert r == {"pins": pin_repo(p, clean).to_json()}
        else:
            assert set(r) == {"error"} and r["error"] == {"code": "observe.unavailable", "message": reason(p, clean)}


def test_pin_many_refuses_a_path_outside_the_roots_alone(mixed):
    declared, good, bad = mixed
    got = ops.run("pin", {"profile": "pin", "roots": [good[0]]}, {"op": "pin_many", "args": {"paths": good}})
    assert got["results"][0] == {"pins": pin_repo(good[0], [good[0]]).to_json()}
    assert "not one of the declared repos" in got["results"][1]["error"]["message"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"paths": "/a"},
        {"paths": []},
        {"paths": ["/a", "/a"]},
        {"paths": ["/a", 1]},
        {"paths": ["/a"], "extra": 1},
        {"paths": [f"/r{i}" for i in range(65)]},
    ],
)
def test_pin_many_refuses_a_malformed_request_as_a_whole(args):
    with pytest.raises(Unavailable, match="bad request"):
        ops.run("pin", {"profile": "pin", "roots": ["/a"]}, {"op": "pin_many", "args": args})


@pytest.mark.parametrize("profile", ["gitdir", "worktree"])
def test_pin_many_runs_only_under_the_pin_profile(profile):
    with pytest.raises(Unavailable, match="bad request"):
        ops.run(profile, {}, {"op": "pin_many", "args": {"paths": ["/a"]}})


def test_pin_many_in_the_child_stays_inside_the_roots(mixed):
    """Through the real child and its audit hook: a path not among the hello's roots is refused, the others pin."""
    declared, good, bad = mixed
    pool = ReaderPool()
    got = pool.call("x", {"profile": "pin", "roots": [good[1]]}, {"op": "pin_many", "args": {"paths": good[::-1]}})
    assert got["results"][0] == {"pins": pin_repo(good[1], [good[1]]).to_json()}
    assert "not one of the declared repos" in got["results"][1]["error"]["message"]


# ---------------------------------------------------------------- forged pins never reach the pin store


def _forge(pins, **paths):
    j = pins.to_json() if hasattr(pins, "to_json") else dict(pins)
    return {k: ({**v, "path": paths[k]} if k in paths else v) for k, v in j.items()}


FORGERIES = [
    {"gitdir": "/etc", "commondir": "/etc"},
    {"worktree": "/etc"},
    {"commondir": "/etc"},
    {"gitdir": "/etc/worktrees/x"},
]


@pytest.mark.parametrize("forged", FORGERIES)
def test_a_batch_answer_with_pins_outside_git_layout_is_never_trusted(mixed, forged):
    """The host checks every pin a child answers against git's layout under the declared roots (the worktree is the
    asked path; gitdir and commondir are ``<declared>/.git`` or ``<declared>/.git/worktrees/<id>``): a child that
    forged one repo's pins cannot get them stored, and the batch falls back to one child per repo."""
    declared, good, bad = mixed

    def forge(got):
        res = [dict(r) for r in got["results"]]
        res[0] = {"pins": _forge(res[0]["pins"], **forged)}
        return {"results": res}

    pool = Spy(forge)
    host = HostGitReader(pool, PinStore())
    res = host.sync(declared)
    clean = [p for p in declared if not p.endswith("/.")]
    assert pool.ops == ["pin_many"] + ["pin"] * len(clean)
    for p in good:
        assert res[p] is None and host.pins.get(p) == pin_repo(p, clean)


@pytest.mark.parametrize("forged", FORGERIES)
def test_a_single_pin_answer_outside_git_layout_is_refused(make_repo, forged):
    r = committed(make_repo, "one")
    real = pin_repo(str(r.path), [str(r.path)])

    class Fake(ReaderPool):
        def call(self, principal, hello, req, **kw):
            return {"pins": _forge(real, **forged)}

    host = HostGitReader(Fake(spawn=lambda: None), PinStore())
    assert host.sync([r.path])[str(r.path)]
    assert host.pins.paths() == []
