"""H1 L5: ``mirror.sync {repo}`` end to end on a real host: the person and an agent (column G) may ask, the agent only
once per 30 s per repository (the answer is shared), tier A has nothing to sync, a repository that is not declared or
not shared yet is a plain refusal. No new error code."""

from __future__ import annotations

import sys

import pytest

from orch.host import mirrorops, sharemirror
from tests.host import sharemirror_helpers
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.sharemirror_helpers import git
from tests.host.test_worktrees import w  # noqa: F401  (the fixture: a real host with a declared repository "proj")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX only"),
]


@pytest.fixture
def shared(w, tmp_path, monkeypatch):  # noqa: F811
    ns = sharemirror_helpers.make_share(tmp_path / "s", monkeypatch)
    monkeypatch.setattr(mirrorops, "_share", lambda call: ns.share)
    monkeypatch.setattr(mirrorops, "GATE", sharemirror.SyncGate())
    ws8 = w.served.wid[:8]
    sharemirror.create_mirror(ns.share, ws8, "proj", str(w.repo))
    ns.ws8 = ws8
    try:
        yield ns
    finally:
        sharemirror_helpers.restore(ns)


def _as_agent(w):  # noqa: F811
    return {"grant": w.served.grant, "session": SESSION}


def _commit(repo, name):
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def test_the_person_syncs_and_gets_the_heads(w, shared):  # noqa: F811
    tip = _commit(w.repo, "n1")
    data = ok(w.c.req("mirror.sync", {"repo": "proj"}))["data"]
    assert data["repo"] == "proj" and data["synced"] is True and data["truncated"] is False
    assert data["heads"][f"refs/heads/{git(w.repo, 'rev-parse', '--abbrev-ref', 'HEAD')}"] == tip


def test_an_agent_may_ask_but_only_once_per_30_seconds_and_the_person_is_never_limited(w, shared):  # noqa: F811
    first = ok(w.c.req("mirror.sync", {"repo": "proj"}, **_as_agent(w)))["data"]
    assert first["synced"] is True
    tip = _commit(w.repo, "n2")
    second = ok(w.c.req("mirror.sync", {"repo": "proj"}, **_as_agent(w)))["data"]
    assert second["synced"] is False and second["heads"] == first["heads"]  # coalesced into the last run
    person = ok(w.c.req("mirror.sync", {"repo": "proj"}))["data"]
    assert person["synced"] is True and tip in person["heads"].values()
    again = ok(w.c.req("mirror.sync", {"repo": "proj"}, **_as_agent(w)))["data"]
    assert again["synced"] is False and again["heads"] == person["heads"]  # the person's run opened the window


def test_a_repo_that_is_not_declared_or_not_shared_yet_is_a_plain_refusal(w, shared):  # noqa: F811
    assert err(w.c.req("mirror.sync", {"repo": "nope"})) == "not_found"
    sharemirror.delete_mirror(shared.share, shared.ws8, "proj")
    res = w.c.req("mirror.sync", {"repo": "proj"})
    assert err(res) == "not_found" and "not shared with agents yet" in str(res)


def test_bad_arguments_and_files_are_refused(w, shared):  # noqa: F811
    assert err(w.c.req("mirror.sync", {})) == "invalid.input"
    assert err(w.c.req("mirror.sync", {"repo": "../x"})) == "invalid.input"
    assert err(w.c.req("mirror.sync", {"repo": "proj", "force": True})) == "invalid.input"


def test_without_an_agent_user_there_is_nothing_to_sync(w):  # noqa: F811
    res = w.c.req("mirror.sync", {"repo": "proj"})
    assert err(res) == "op.unsupported" and "nothing is shared" in str(res)


def test_an_integrity_failure_is_observe_unavailable_with_the_old_mirror_kept(w, shared):  # noqa: F811
    before = ok(w.c.req("mirror.sync", {"repo": "proj"}))["data"]["heads"]
    import os
    import subprocess

    tree = git(w.repo, "rev-parse", "HEAD^{tree}")
    bad = subprocess.run(
        ["git", "-C", str(w.repo), "hash-object", "-w", "--literally", "-t", "commit", "--stdin"],
        input=f"tree {tree}\n\nno author\n", capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(w.repo.parent)},
    ).stdout.strip()  # fmt: skip
    git(w.repo, "update-ref", "refs/heads/broken", bad)
    res = w.c.req("mirror.sync", {"repo": "proj"})
    assert err(res) == "observe.unavailable" and "integrity" in str(res)
    final = shared.share.mirror_path(shared.ws8, "proj")
    now = {
        r.split()[1]: r.split()[0] for r in git(final, "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
    }
    assert now == before
