"""The H6c reader arguments (§0.8, §0.8.5): the ``host_pinned`` base, ``since`` over the caller's approvals, and
``head``/``base`` authorized by ranges the reader derives itself."""

from __future__ import annotations

import pytest

from orch.gitread import local
from orch.gitread.errors import NotFound

ID = "local:app"


def push(sha, before=None, repo=ID, ref="refs/heads/feat", bref=None, brepo=None):
    b = None if before is None else {"repo": brepo or repo, "ref": bref or ref, "sha": before}
    return {"repo": repo, "ref": ref, "sha": sha, "before": b}


@pytest.fixture
def scene(make_repo):
    """main: m1; feat: m1 - c2 - c3 - c3b - c4 (head)."""
    r = make_repo()
    r.write("a.txt", "1\n")
    m1 = r.commit("m1")
    r.git("checkout", "-q", "-b", "feat")
    shas = {"m1": m1}
    for n in ("c2", "c3", "c3b", "c4"):
        r.write(f"{n}.txt", n + "\n")
        shas[n] = r.commit(n)
    r.git("checkout", "-q", "main")
    r.git("commit", "-q", "--allow-empty", "-m", "main moved")
    shas["m2"] = r.sha("main")
    return r, shas


def d(r, **ext):
    return local.diff(r.path, branch="feat", default_branch=None, ext={"name": "app", **ext})


def test_no_ext_and_a_bare_name_change_nothing(scene):
    r, s = scene
    plain = local.diff(r.path, branch="feat", default_branch=None)
    assert plain["base_sha"] == s["m1"] and plain["base_source"] == "local_ref" and plain["observed_base"] is None
    got = d(r)
    assert {k: v for k, v in got.items() if k != "observed_base"} == {
        k: v for k, v in plain.items() if k != "observed_base"
    }
    assert got["observed_base"] == s["m1"]  # what the host may pin


def test_host_pinned_wins_while_below_the_head_and_the_default_tip_and_counts_how_far_main_moved(scene):
    r, s = scene
    got = d(r, pins=[{"repo": ID, "sha": s["m1"]}])
    assert (got["base_sha"], got["base_source"], got["base_moved"]) == (s["m1"], "host_pinned", 0)
    # a pin the default branch does not contain (an agent moved main there, got it pinned, moved main back) is ignored
    assert d(r, pins=[{"repo": ID, "sha": s["c2"]}])["base_source"] == "local_ref"
    # main takes the branch's first commit: the pin stays below it, and the commits it hides are counted
    r.git("checkout", "-q", "main")
    r.git("merge", "-q", "--no-edit", "feat~3")  # feat~3 = c2
    pin = [{"repo": ID, "sha": s["m1"]}]
    got = d(r, pins=pin)
    assert got["base_source"] == "host_pinned" and got["base_sha"] == s["m1"] and got["base_moved"] == 1
    # an explicit base equal to the pinned base reports the same, not a bare 0
    got = d(r, pins=pin, base=s["m1"])
    assert (got["base_source"], got["base_moved"]) == ("host_pinned", 1)
    # a pin for another identity, one that is no ancestor of the head, or a missing object is not used
    assert d(r, pins=[{"repo": "local:other", "sha": s["m1"]}])["base_source"] == "local_ref"
    assert d(r, pins=[{"repo": ID, "sha": s["m2"]}])["base_source"] in ("local_ref", "first_parent")
    assert d(r, pins=[{"repo": ID, "sha": "0" * 40}])["base_source"] == "local_ref"


def test_a_head_or_base_of_the_wrong_length_is_the_same_not_found(scene):
    r, s = scene
    texts = set()
    for bad in ({"head": "a" * 64}, {"base": "a" * 64}, {"head": "a" * 40}, {"base": "a" * 39}):
        with pytest.raises(NotFound) as e:
            d(r, **bad)
        texts.add(e.value.reason)
    assert texts == {"not found for this ticket"}


def test_since_takes_the_newest_approval_that_names_this_repo_and_is_an_ancestor(scene):
    r, s = scene
    approvals = [
        {"repo": "local:other", "sha": s["c3"]},  # another repo identity
        {"repo": ID, "sha": s["m2"]},  # not an ancestor of the head
        {"repo": ID, "sha": s["c3"]},
        {"repo": ID, "sha": s["c2"]},
    ]
    got = d(r, since=True, approvals=approvals)
    assert (got["base_sha"], got["base_source"], got["head_sha"]) == (s["c3"], "approved", s["c4"])
    assert [c["sha"] for c in got["commits"]] == [s["c4"], s["c3b"]]
    for none in ([], [{"repo": ID, "sha": s["m2"]}], [{"repo": "local:other", "sha": s["c3"]}]):
        with pytest.raises(NotFound, match="no approval of yours"):
            d(r, since=True, approvals=none)


def test_head_and_base_are_authorized_by_ranges_the_reader_derives(scene):
    r, s = scene
    pushes = [push(s["c4"], s["c2"]), push(s["c2"])]
    ok = d(r, head=s["c3"], pushes=pushes)  # inside the default range m1..c4
    assert (ok["head_sha"], ok["base_sha"], ok["base_source"]) == (s["c3"], s["m1"], "local_ref")
    got = d(r, head=s["c3"], base=s["c2"], pushes=pushes)  # the first parent
    assert (got["base_sha"], got["base_source"]) == (s["c2"], "first_parent")
    got = d(r, base=s["c2"], pushes=pushes)  # a pushed base, observed head
    assert (got["head_sha"], got["base_sha"], got["base_source"]) == (s["c4"], s["c2"], "pushed")
    got = d(r, base=s["c3b"], pushes=pushes)
    assert got["base_source"] == "first_parent"
    got = d(r, base=s["m1"], pushes=pushes)
    assert got["base_source"] == "local_ref"
    for bad in (
        {"head": s["m1"]},  # an ancestor of the range, outside it
        {"head": s["m2"]},  # exists, outside every range
        {"head": "1" * 40},  # does not exist
        {"base": s["c3"]},  # an ancestor of head that no rule offers
        {"head": s["c2"], "base": s["c3"]},  # not an ancestor of the head
        {"base": s["m2"]},
    ):
        with pytest.raises(NotFound) as e:
            d(r, pushes=pushes, **bad)
        assert e.value.reason == "not found for this ticket"  # one text for exists and does not exist


def test_a_push_of_another_identity_or_ref_authorizes_nothing(scene):
    r, s = scene
    r.git("branch", "other", s["m2"])
    r.git("checkout", "-q", "feat")
    r.write("late.txt", "x\n")
    late = r.commit("late")
    r.git("reset", "-q", "--hard", s["c4"])  # force-pushed away: `late` is no longer on the branch
    with pytest.raises(NotFound):
        d(r, head=late, pushes=[])
    assert d(r, head=late, pushes=[push(late, s["c4"])])["head_sha"] == late  # a push by this ticket's identity
    with pytest.raises(NotFound):
        d(r, head=late, pushes=[push(late, s["c4"], repo="local:elsewhere")])
    # a `before` qualifies only with its own identity and the push's ref
    base = d(r, base=s["c3"], pushes=[push(s["c4"], s["c3"])])
    assert base["base_source"] == "pushed"
    with pytest.raises(NotFound):
        d(r, base=s["c3"], pushes=[push(s["c4"], s["c3"], brepo="local:elsewhere")])
    with pytest.raises(NotFound):
        d(r, base=s["c3"], pushes=[push(s["c4"], s["c3"], bref="refs/heads/main")])


def test_a_caller_endpoint_never_enlarges_a_range(scene):
    r, s = scene
    # the pushed head `c2` has its own default range m1..c2: c3 is not in it, but is in the observed head's range;
    # a push of c3b is a root with the range m1..c3b. A base chosen by the caller adds nothing to either.
    pushes = [push(s["c2"])]
    with pytest.raises(NotFound):
        d(r, head=s["m2"], base=s["m1"], pushes=pushes)
    with pytest.raises(NotFound):
        d(r, head=s["c4"], base=s["m2"], pushes=pushes)
