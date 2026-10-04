from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.testing import FakeRunner, ProviderContract, fake_workspace
from orch_wiki.diffs import BranchDiffs

MANIFEST = load_manifest(Path(__file__).resolve().parents[1])
REPO = "acme-energy-data"
BRANCH = "feature/DEMO-0003-move-loader"
OLD = "feature/DEMO-0002-old"


def git(*args):
    return ["git", "-C", "*", *args]


SYMREF = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")


def revparse(ref):
    return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def set_meta(fw, tid, **meta):
    from orch.core import store
    path, t = store.load(fw.ws, tid)
    t.meta.update(meta)
    store.save(fw.ws, t, old_path=path)


@pytest.fixture
def fw(tmp_path):
    fw = fake_workspace(tmp_path / "w", repos={REPO: {"path": "."}}, tickets=[
        {"title": "Open one", "status": "open"}, {"title": "Old one", "status": "done"},
        {"title": "Move loader", "status": "testing"}])
    set_meta(fw, "DEMO-0001", branches={REPO: "feature/DEMO-0001-open"})
    set_meta(fw, "DEMO-0002", branches={REPO: OLD})
    set_meta(fw, "DEMO-0003", branches={REPO: BRANCH})
    return fw


def gone(*refs):
    r = FakeRunner().add(SYMREF, stdout="origin/main\n")
    for ref in refs:
        r.add(revparse(ref), returncode=1).add(revparse(f"origin/{ref}"), returncode=1)
    return r


def runner():
    return (gone(OLD).add(revparse(BRANCH), stdout="abc123\n")
            .add(git("diff", "--name-only", f"origin/main...{BRANCH}"), stdout="src/acme/ingest/loader.py\nREADME.md\n"))


def ctx_for(fw, r):
    return fw.provider_context(MANIFEST, runner=r)


def test_only_testing_and_done_tickets_are_checked(fw):
    r = runner()
    snap = BranchDiffs().fetch(ctx_for(fw, r), "tickets", None)
    items = {i["ticket"]: i for i in snap.items}
    assert sorted(items) == ["DEMO-0002", "DEMO-0003"]
    assert items["DEMO-0003"]["files"] == ["src/acme/ingest/loader.py", "README.md"]
    assert (items["DEMO-0003"]["role"], items["DEMO-0003"]["status"], items["DEMO-0003"]["base"]) == ("info", "testing", "origin/main")
    assert items["DEMO-0002"]["text"] == "branch not found locally" and items["DEMO-0002"]["files"] == []
    assert all(c[:2] == ("git", "-C") and c[3] in ("symbolic-ref", "rev-parse", "diff") for c in r.calls)


def test_a_merged_branch_keeps_the_earlier_files(fw):
    first = BranchDiffs().fetch(ctx_for(fw, runner()), "tickets", None)
    again = BranchDiffs().fetch(ctx_for(fw, gone(OLD, BRANCH)), "tickets", first)
    item = next(i for i in again.items if i["ticket"] == "DEMO-0003")
    assert item["files"] == ["src/acme/ingest/loader.py", "README.md"]
    assert item["text"] == "2 file(s) from an earlier check (branch not found locally)"


def test_default_branch_from_the_config(tmp_path):
    fw = fake_workspace(tmp_path / "w", repos={REPO: {"path": ".", "default_branch": "develop"}},
                        tickets=[{"title": "T", "status": "testing"}])
    set_meta(fw, "DEMO-0001", branches={REPO: "feature/x"})
    r = FakeRunner().add(revparse("feature/x")).add(git("diff", "--name-only", "develop...feature/x"), stdout="a.py\n")
    snap = BranchDiffs().fetch(ctx_for(fw, r), "tickets", None)
    assert snap.items[0]["files"] == ["a.py"] and not any(c[3] == "symbolic-ref" for c in r.calls)


@pytest.mark.parametrize("branches, text", [
    ({"elsewhere": "feature/x"}, "repo elsewhere is not in git.repos"),
    ({REPO: "-rf"}, "branch name not readable"),
    ({REPO: "a..b"}, "branch name not readable"),
])
def test_unreadable_refs_never_reach_git(fw, branches, text):
    set_meta(fw, "DEMO-0003", branches=branches)
    r = gone(OLD)
    snap = BranchDiffs().fetch(ctx_for(fw, r), "tickets", None)
    item = next(i for i in snap.items if i["ticket"] == "DEMO-0003")
    assert item["text"] == text and item["files"] == []
    assert not any("-rf" in c or any("a..b" in a for a in c) for c in r.calls)


def test_not_a_git_repo_is_a_message(fw):
    bad = {"returncode": 128, "stderr": "fatal: not a git repository (or any of the parent directories): .git\n"}
    r = FakeRunner().add(SYMREF, **bad).add(revparse(BRANCH), **bad).add(revparse(OLD), **bad)
    snap = BranchDiffs().fetch(ctx_for(fw, r), "tickets", None)
    assert snap.items and all("not a git repository" in i["text"] for i in snap.items)


class TestBranchDiffsContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return BranchDiffs()

    @pytest.fixture
    def provider_ctx(self, fw):
        return ctx_for(fw, runner())
