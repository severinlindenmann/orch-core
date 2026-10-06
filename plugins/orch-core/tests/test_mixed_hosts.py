"""#165: per-repo git provider, the `orch check` commit baseline, PR state on `orch link`, and ctx.run's env."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from addon_fixtures import GOOD
from orch.addons.api import AddonContext, workspace_repos
from orch.addons.manifest import parse_manifest
from orch.addons.runner import AddonRunError, RunResult, SubprocessRunner
from orch.config.load import repo_git, validate_schema
from orch.core import store
from orch.core.check import run_checks
from orch.core.prlink import review_url
from orch.core.rules import render_rules
from orch.errors import UsageError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
MIXED = {"type": "github", "repos": {"hub": {"path": "hub"},
                                     "meta": {"path": "meta", "type": "bitbucket-server",
                                              "base_url": "https://bitbucket.example.com/"}}}


def _git(path, *args, env=None):
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, env={**os.environ, **(env or {})})


# -- per-repo git provider ---------------------------------------------------------------------------------------

def test_repo_git_falls_back_to_the_workspace():
    cfg = {"git": {"type": "gitlab-selfhosted", "base_url": "https://git.example.com", "repos": {
        "a": {}, "b": {"type": "github"}, "c": {"base_url": "https://other.example.com"},
        "d": {"type": "gitlab-selfhosted"}}}}
    assert repo_git(cfg, "a") == ("gitlab-selfhosted", "https://git.example.com")
    assert repo_git(cfg, "b") == ("github", "")  # another host's base_url is never inherited
    assert repo_git(cfg, "c") == ("gitlab-selfhosted", "https://other.example.com")
    assert repo_git(cfg, "d") == ("gitlab-selfhosted", "https://git.example.com")
    assert repo_git(cfg, None) == ("gitlab-selfhosted", "https://git.example.com")
    assert repo_git({}, "x") == ("github", "")


def test_schema_checks_the_repo_provider_and_the_baseline():
    base = {"schema": 1, "customer": "a", "id": {"prefix": "L", "pad": 4}}
    assert validate_schema({**base, "git": MIXED, "check": {"since": "2026-10-06"}}) == []
    assert validate_schema({**base, "check": {"since": {"hub": "a1b2c3d", "meta": "2026-01-31"}}}) == []
    bad = validate_schema({**base, "git": {"repos": {"x": {"type": "", "base_url": "bitbucket.example.com"}}},
                           "check": {"since": "last week"}})
    assert any(e.startswith("check/since") for e in bad)
    assert any(e.startswith("git/repos/x/type") for e in bad) and any(e.startswith("git/repos/x/base_url") for e in bad)


def test_workspace_repos_carry_their_provider(configure):
    ws = configure(git=MIXED)
    by = {r.name: r for r in workspace_repos(ws)}
    assert (by["harness"].git_type, by["hub"].git_type, by["meta"].git_type) == ("github", "github", "bitbucket-server")
    assert by["meta"].base_url == "https://bitbucket.example.com"


@pytest.mark.parametrize("host, path, kind, base, want", [
    ("bitbucket.example.com", "dis/meta", "bitbucket-server", "",
     "https://bitbucket.example.com/projects/DIS/repos/meta/pull-requests/7"),
    ("bitbucket.example.com", "scm/dis/meta", "bitbucket-server", "https://bitbucket.example.com",
     "https://bitbucket.example.com/projects/DIS/repos/meta/pull-requests/7"),
    ("bb.example.com", "bitbucket/scm/~sev/meta", "bitbucket-server", "https://bb.example.com/bitbucket",
     "https://bb.example.com/bitbucket/users/sev/repos/meta/pull-requests/7"),
    ("bitbucket.org", "acme/meta", "bitbucket", "", "https://bitbucket.org/acme/meta/pull-requests/7"),
    ("git.example.com", "g/app", "gitlab-selfhosted", "", "https://git.example.com/g/app/-/merge_requests/7"),
    ("gitlab.example.com", "g/app", "github", "", "https://gitlab.example.com/g/app/-/merge_requests/7"),
    ("github.com", "acme/hub", "github", "", "https://github.com/acme/hub/pull/7"),
])
def test_review_url_follows_the_repo_provider(host, path, kind, base, want):
    assert review_url(host, path, 7, kind, base) == want


@needs_git
def test_link_a_pr_number_in_a_bitbucket_server_repo(configure, ws_root, agent):
    for name, url in (("hub", "git@github.com:acme/hub.git"), ("meta", "ssh://git@bitbucket.example.com:7999/dis/meta.git")):
        (ws_root / name).mkdir()
        _git(ws_root / name, "init", "-q")
        _git(ws_root / name, "remote", "add", "origin", url)
    ws = configure(git=MIXED)
    from orch.core.ops import Ops
    ops = Ops(ws, agent)
    t = ops.new("x")
    ops.link(t.id, repo="meta", pr="12")
    ops.link(t.id, repo="hub", pr="3")
    prs = store.load(ws, t.id)[1].meta["prs"]
    assert [p["url"] for p in prs] == ["https://bitbucket.example.com/projects/DIS/repos/meta/pull-requests/12",
                                       "https://github.com/acme/hub/pull/3"]
    u = ops.new("y")
    ops.link(u.id, pr="https://bitbucket.example.com/projects/DIS/repos/meta/pull-requests/40")
    assert store.load(ws, u.id)[1].meta["prs"][0]["repo"] == "meta"


def test_rules_name_repos_on_another_host(configure):
    assert "git: github (meta: bitbucket-server) ·" in render_rules(configure(git=MIXED).config)
    assert "git: github ·" in render_rules(configure().config)


def test_doctor_lists_hosts_only_for_mixed_workspaces(configure, ws_root):
    from orch.onboarding import doctor
    configure(git=MIXED)
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["git-hosts"].ok and "meta bitbucket-server (https://bitbucket.example.com)" in checks["git-hosts"].message
    configure(git={"repos": {"meta": {"type": "svn"}}})
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["git-hosts"].ok is False and "meta (svn)" in checks["git-hosts"].message
    configure(git={"repos": {"hub": {}}})
    assert "git-hosts" not in {c.code for c in doctor(ws_root)}


# -- orch check baseline -----------------------------------------------------------------------------------------

@pytest.fixture
def history(ws_root):
    """hub: one old commit (2020) citing ABC-1, then one new commit citing ABC-2; returns the new commit's sha."""
    repo = ws_root / "hub"
    repo.mkdir()
    _git(repo, "init", "-q")
    old = {"GIT_AUTHOR_DATE": "2020-01-01T12:00:00", "GIT_COMMITTER_DATE": "2020-01-01T12:00:00"}
    _git(repo, "commit", "--allow-empty", "-q", "-m", "ABC-1 Old work", env=old)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    _git(repo, "commit", "--allow-empty", "-q", "-m", "ABC-2 New work")
    return base


def _unlinked(ws):
    return sorted(f.message.split(" cites ")[1].split(",")[0] for f in run_checks(ws) if f.code == "commit-unlinked-key")


TRACKERS = [{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "u/{key}"}]


@needs_git
def test_without_a_baseline_every_commit_is_checked(configure, history):
    assert _unlinked(configure(git={"repos": {"hub": {}}}, external_trackers=TRACKERS)) == ["ABC-1", "ABC-2"]


@needs_git
@pytest.mark.parametrize("since", ["2021-06-01", {"hub": "2021-06-01"}, "commit"])
def test_a_baseline_checks_only_newer_commits(configure, history, since):
    since = history[:12] if since == "commit" else since
    ws = configure(git={"repos": {"hub": {}}}, external_trackers=TRACKERS, check={"since": since})
    assert _unlinked(ws) == ["ABC-2"]


@needs_git
def test_a_baseline_for_another_repo_leaves_this_one_alone(configure, history):
    ws = configure(git={"repos": {"hub": {}}}, external_trackers=TRACKERS, check={"since": {"other": "2021-06-01"}})
    assert _unlinked(ws) == ["ABC-1", "ABC-2"]


@needs_git
def test_a_baseline_commit_not_in_the_repo_says_so(configure, history):
    ws = configure(git={"repos": {"hub": {}}}, external_trackers=TRACKERS, check={"since": "deadbeefdeadbeef"})
    found = [f for f in run_checks(ws) if f.code in ("check-since", "commit-unlinked-key")]
    assert [f.code for f in found] == ["check-since"] and "deadbeefdeadbeef" in found[0].message


def test_init_sets_the_baseline_to_today(tmp_path, monkeypatch):
    from orch.cli import run
    from orch.clock import now
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "acme", "--no-instructions"]) == 0
    cfg = json.loads((tmp_path / "orchestrator" / "config.json").read_text(encoding="utf-8"))
    assert cfg["check"] == {"since": now().date().isoformat()}


# -- PR state on orch link ---------------------------------------------------------------------------------------

URL = "https://github.com/acme/energy-data/pull/9"


@pytest.fixture
def origin(ws_root):
    _git(ws_root, "init", "-q")
    _git(ws_root, "remote", "add", "origin", "git@github.com:acme/energy-data.git")


def test_a_linked_pr_is_unknown_until_told(ws, aops, origin):
    t = aops.new("x")
    aops.link(t.id, pr=URL)
    assert store.load(ws, t.id)[1].meta["prs"] == [{"repo": "harness", "url": URL, "state": "unknown"}]
    aops.link(t.id, pr=URL, pr_state="merged")
    assert store.load(ws, t.id)[1].meta["prs"] == [{"repo": "harness", "url": URL, "state": "merged"}]


def test_pr_state_is_checked(ws, aops, origin):
    t = aops.new("x")
    with pytest.raises(UsageError, match="unknown PR state"):
        aops.link(t.id, pr=URL, pr_state="closed")
    with pytest.raises(UsageError, match="--state needs --pr"):
        aops.link(t.id, branch="b", repo="harness", pr_state="open")


def test_cli_link_state(ws, aops, origin, monkeypatch):
    from orch.cli import run
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    t = aops.new("x")
    assert run(["link", t.id, "--pr", URL, "--state", "declined"]) == 0
    assert store.load(ws, t.id)[1].meta["prs"][0]["state"] == "declined"


def test_the_placeholder_state_is_not_shown():
    from orch.dashboard.routes_ticket import _pr_label
    assert _pr_label({"repo": "hub", "url": URL, "state": "unknown"}) == "hub #9"
    assert _pr_label({"repo": "hub", "url": URL, "state": "merged"}) == "hub #9 · merged"


# -- ctx.run env -------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout, env=None):
        self.calls.append((tuple(argv), env))
        return RunResult(tuple(argv), 0, "", "")


def _ctx(ws, runner, **over):
    m = parse_manifest({**GOOD, **over})
    return AddonContext(ws, m.name, manifest=m, runner=runner).provider_context()


def test_run_env_takes_only_declared_names(ws):
    rec = Recorder()
    ctx = _ctx(ws, rec, env=["GH_TOKEN"])
    ctx.run(["git", "status"], env={"GH_TOKEN": "t"})
    ctx.run(["git", "status"])
    assert rec.calls == [(("git", "status"), {"GH_TOKEN": "t"}), (("git", "status"), None)]
    for env in ({"PATH": "/tmp"}, {"ORCH_HOME": "/x"}, {"GH_TOKEN": 3}, ["GH_TOKEN"]):
        with pytest.raises(AddonRunError):
            ctx.run(["git", "status"], env=env)
    assert len(rec.calls) == 2


def test_subprocess_runner_adds_the_call_env(tmp_path, monkeypatch):
    exe = Path(sys.executable)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("GH_TOKEN", "from-the-shell")
    runner = SubprocessRunner(env_names=("GH_TOKEN",), cwd=tmp_path)
    show = [exe.name, "-c", "import os; print(os.environ.get('GH_TOKEN'), os.environ.get('OTHER'))"]
    assert runner(show, 10).stdout.split() == ["from-the-shell", "None"]
    assert runner(show, 10, env={"GH_TOKEN": "per-call", "OTHER": "x"}).stdout.split() == ["per-call", "None"]
