"""``diff {since:"my_approval"}`` and ``{head, base}`` on the workspace socket, after a real code approval through the
host (H6c, §0.8): rita, a maintainer who did not work on the ticket, approves ``code``; only rita's own approval is a
base, for her (column P) and never for the owner's grant (column G) or an agent without a person (N)."""

from __future__ import annotations

import pytest

from tests.host.dispatch_helpers import err, ok
from tests.host.test_dispatch_matrix import as_agent_class
from tests.host.test_tree_check_host import _world

pytestmark = pytest.mark.slow


@pytest.fixture
def rita_world(tmp_path, monkeypatch):
    yield from _world(tmp_path, monkeypatch, "rita")


def approve_then_commit(w):
    h = w.human
    prepared = h.prepare("approve", {"ref": w.key, "gate": "code"})
    ok(h.submit_res(prepared["request"]["id"], h.sign(prepared).events))
    git = w.hws.git
    approved = git("rev-parse", "feat/x")
    shas = [approved]
    for n in ("one", "two"):
        (w.repo / f"{n}.txt").write_text(n + "\n")
        git("add", f"{n}.txt")
        git("commit", "-qm", n)
        shas.append(git("rev-parse", "HEAD"))
    return shas


def test_since_my_approval_is_the_callers_own_approval_only(rita_world):
    w = rita_world
    approved, one, two = approve_then_commit(w)
    c = w.served.client()  # rita, column P
    res = ok(c.req("diff", {"ref": w.key, "since": "my_approval"}))["data"]
    (src,) = res["sources"]
    assert (src["base_sha"], src["head_sha"], src["base_source"]) == (approved, two, "approved")
    assert {x["sha"] for x in src["commits"]} == {one, two}
    assert [f["path"] for f in res["files"]] == ["one.txt", "two.txt"] and set(res) == {
        "sources", "files", "unified", "truncated",
    }  # fmt: skip
    # the owner's grant (column G, person = the owner) must not borrow rita's approval
    from tests.host.dispatch_helpers import SESSION

    other = w.served.client().req("diff", {"ref": w.key, "since": "my_approval"}, grant=w.served.grant, session=SESSION)
    assert err(other) == "not_found" and "no approval of yours" in other["error"]["message"]
    as_agent_class(w.served)
    assert err(w.served.client().req("diff", {"ref": w.key, "since": "my_approval"})) == "not_found"


def test_head_and_base_after_a_push_are_authorized_by_the_ticket(rita_world):
    w = rita_world
    approved, one, two = approve_then_commit(w)
    c = w.served.client()
    # a `show` observes the new head: a push approved -> two
    ok(c.req("show", {"ref": w.key}))
    got = ok(c.req("diff", {"ref": w.key, "repo": "proj", "base": approved}))["data"]["sources"][0]
    assert (got["base_source"], got["base_sha"], got["head_sha"]) in {
        ("pushed", approved, two),
        ("local_ref", approved, two),
    }
    got = ok(c.req("diff", {"ref": w.key, "repo": "proj", "head": two, "base": one}))["data"]["sources"][0]
    assert (got["head_sha"], got["base_sha"], got["base_source"]) == (two, one, "first_parent")
    # this repo has no default branch: the observed head's range is its last commit, so `one` is no head to ask for
    assert err(c.req("diff", {"ref": w.key, "repo": "proj", "head": one})) == "not_found"
    assert err(c.req("diff", {"ref": w.key, "repo": "proj", "head": "c" * 40})) == "not_found"


def test_a_revoked_approver_device_leaves_no_approval_for_her(rita_world):
    w = rita_world
    approve_then_commit(w)
    c = w.served.client()
    ok(c.req("diff", {"ref": w.key, "since": "my_approval"}))
    w.served.lab.revoke_device(w.human.person)
    res = w.served.client().req("diff", {"ref": w.key, "since": "my_approval"})
    assert err(res) == "not_found" and "no approval of yours" in res["error"]["message"]


def test_an_approval_revoked_while_the_diff_runs_is_refused(rita_world, monkeypatch):
    """The approval resolved before the read is resolved again after it, on the success path (the failing path
    has its own test)."""
    import threading
    import time

    w = rita_world
    approve_then_commit(w)
    reader = w.served.service.capabilities().git.reader
    real = type(reader).diff
    started = threading.Event()

    def slow(self, *a, **k):
        started.set()
        time.sleep(1.5)
        return real(self, *a, **k)

    monkeypatch.setattr(type(reader), "diff", slow)
    out = {}
    c = w.served.client()
    t = threading.Thread(target=lambda: out.setdefault("r", c.req("diff", {"ref": w.key, "since": "my_approval"})))
    t.start()
    assert started.wait(10)
    w.served.lab.revoke_device(w.human.person)
    t.join(30)
    monkeypatch.undo()
    assert err(out["r"]) == "not_found"


def test_an_approval_revoked_while_a_failing_diff_runs_is_refused(rita_world, monkeypatch):
    import threading
    import time

    from orch.gitread.errors import Unavailable

    w = rita_world
    approve_then_commit(w)
    reader = w.served.service.capabilities().git.reader
    started = threading.Event()

    def fail(self, *a, **k):
        started.set()
        time.sleep(1.5)
        raise Unavailable("the reader child died")

    monkeypatch.setattr(type(reader), "diff", fail)
    out = {}
    c = w.served.client()
    t = threading.Thread(target=lambda: out.setdefault("r", c.req("diff", {"ref": w.key, "since": "my_approval"})))
    t.start()
    assert started.wait(10)
    w.served.lab.revoke_device(w.human.person)
    t.join(30)
    monkeypatch.undo()
    assert err(out["r"]) == "not_found"
