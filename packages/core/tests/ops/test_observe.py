"""``orch.store.observe``: the host's look at the linked repositories (F1 5.7), before submit, show and wait."""

from __future__ import annotations

import os
import subprocess

from tests.ops.test_task_ac import claimed, fill_and_approve, py

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


def types(ws):
    return [e["type"] for e in ws.events("1")]


def test_submit_on_a_repo_linked_ticket_works_end_to_end(ws, cli):
    ws.repo()
    head = ws.git("rev-parse", "HEAD")
    claimed(cli)
    fill_and_approve(ws, cli)
    assert cli("set", "1", LINKS).code == 0
    cli("section", "set", "verification", "-m", "run it")
    assert cli("task", "done", "T1", "--run").code == 0
    assert ws.events("1")[-1]["receipt"]["commit"] == head
    assert "branch.pushed" not in types(ws)
    r = cli("submit")  # observes first: the source list now holds the head the receipt names
    assert r.code == 0, r.err
    ev = [e for e in ws.events("1") if e["type"] == "branch.pushed"]
    assert len(ev) == 1 and ev[0]["actor"] == {"kind": "host"}
    assert (ev[0]["repo_name"], ev[0]["repo_id"], ev[0]["ref"], ev[0]["sha"], ev[0]["before"]) == (
        "proj",
        "local:proj",
        "refs/heads/feat/x",
        head,
        None,
    )
    v = ws.view("1")
    assert v.status == "testing" and all(a.evidence for a in v.acceptance)
    assert [dict(x) for x in v.source_list] == [{"repo": "local:proj", "ref": "refs/heads/feat/x", "sha": head}]


def test_a_new_commit_is_observed_on_the_next_show_and_old_receipts_stop_counting(ws, cli):
    repo = ws.repo()
    claimed(cli)
    cli("ac", "add", "a")
    cli("task", "add", "t", "--verify", py("print(1)"), "--proves", "AC1")
    cli("set", "1", LINKS)
    cli("task", "done", "T1", "--run")
    cli("show")
    assert ws.view("1").acceptance[0].evidence == ("task:T1",)
    (repo / "impl.txt").write_text("changed\n")
    ws.git("commit", "-qam", "two")
    new = ws.git("rev-parse", "HEAD")
    assert cli("show").code == 0
    ev = [e for e in ws.events("1") if e["type"] == "branch.pushed"]
    assert len(ev) == 2 and ev[1]["sha"] == new and ev[1]["before"]["sha"] == ev[0]["sha"]
    assert ws.view("1").acceptance[0].evidence == ()  # the receipt names the old commit
    cli("show")
    assert len([e for e in ws.events("1") if e["type"] == "branch.pushed"]) == 2  # nothing changed: nothing appended


def test_wait_observes_too_and_a_missing_branch_or_repo_is_skipped(ws, cli):
    ws.repo()
    claimed(cli)
    cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"no-such-branch"}}')
    cli("show")
    assert "branch.pushed" not in types(ws)
    cli("set", "1", LINKS)
    cli("ask", "Which?")  # something open for a person: the wait waits (else wait.incomplete)
    assert cli("wait", "--timeout", "1").code == 0
    assert "branch.pushed" in types(ws)


def test_a_read_only_store_observes_nothing(ws, cli):
    import shutil

    ws.repo()
    claimed(cli)
    cli("set", "1", LINKS)
    uid = ws.uid("1")
    shutil.rmtree(ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "keys")
    assert cli("show").code == 0
    assert "branch.pushed" not in [e["type"] for e in ws.read_events(uid)]


#: the variables orch.gitsafe.isolated_env sets itself
_OURS = {
    "LANG", "LC_ALL", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH",
    "GIT_ATTR_NOSYSTEM", "GIT_NO_REPLACE_OBJECTS", "GIT_PAGER", "GIT_EDITOR", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_GRAFT_FILE",
}  # fmt: skip


def test_git_environment_is_scrubbed(tmp_path, monkeypatch):
    from orch import gitsafe
    from orch.store import observe

    monkeypatch.setenv("GIT_DIR", "/nowhere")
    monkeypatch.setenv("git_work_tree", "/nowhere")
    env = gitsafe.isolated_env(os.environ)  # the environment every remaining isolated git runs in (#546)
    assert "GIT_DIR" not in env and "git_work_tree" not in env
    assert {k for k in env if k.upper().startswith("GIT_")} <= _OURS  # only what the isolation sets itself
    assert observe.head(tmp_path) is None  # not a repository, and GIT_DIR did not make it one


def test_credentials_in_a_remote_are_never_stored_printed_or_passed_on(ws, cli, monkeypatch, tmp_path):
    secret = "ghp_SECRETTOKEN0123456789"
    ws.repo()
    ws.git("remote", "add", "origin", f"https://alice:{secret}@GitHub.com/acme/Repo.git")
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setenv("GIT_ASKPASS", "/bin/echo")
    claimed(cli)
    cli("set", "1", LINKS)
    r = cli("show")
    assert r.code == 0 and secret not in r.out + r.err
    ev = [e for e in ws.events("1") if e["type"] == "branch.pushed"]
    assert ev[0]["repo_id"] == "local:proj"  # non-git userinfo is not a repo identity
    for p in ws.root.rglob("*"):
        if p.is_file() and ".venv" not in str(p):
            assert secret.encode() not in p.read_bytes(), p
    # a remote that is not canonical even without credentials becomes local:<name>, never the raw URL
    ws.git("remote", "set-url", "origin", f"ssh://bob:{secret}@host:2222/odd path")
    from orch.store import observe

    assert observe.repo_identity(ws.tmp / "proj", "proj") == "local:proj"


def test_git_runs_with_an_allow_listed_environment(monkeypatch):

    for k in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "ORCH_GRANT", "GIT_ASKPASS"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("LC_ALL", "C")
    from orch import gitsafe

    env = gitsafe.isolated_env(os.environ)  # the environment every remaining isolated git runs in (#546)
    assert all(k in {"PATH", "HOME", "LANG", "TMPDIR", "USER", "LOGNAME", "XDG_CONFIG_HOME"} or k in _OURS for k in env)
    assert env["LC_ALL"] == "C"


def test_git_runs_without_a_controlling_terminal(ws, tmp_path):
    """A clean filter set in the repository's own config would run inside ``git status``: since #565 observe runs no
    git at all (orch's reader in-process), so the filter never runs and the answer is the real one: the same bytes are
    clean (before, #546's gate refused the repository: ``None``, unknown)."""
    import sys
    import time

    from orch.store import observe

    repo = ws.repo()
    marker = tmp_path / "marker"
    script = tmp_path / "filter.py"
    script.write_text(
        "import sys, os\n"
        "try:\n    open('/dev/tty', 'w').close(); r = 'tty'\nexcept OSError:\n    r = 'no tty'\n"
        f"open({str(marker)!r}, 'w').write(r)\nsys.stdout.write(sys.stdin.read())\n"
    )
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=probe\n")
    ws.git("config", "filter.probe.clean", f"{sys.executable} {script}")
    time.sleep(1.1)
    (repo / "impl.txt").write_text("broken\n")  # same content, new mtime: git runs the filter to compare
    assert observe.dirty(repo) == ""  # same bytes as committed: clean, read without git
    assert not marker.exists()
    # positive control: plain git status runs that filter, so the assertions above can fail
    done = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, check=False)
    assert done.returncode == 0 and marker.exists()


def test_observe_refuses_remote_mismatch_and_accepts_ascii_case_match(ws, cli):
    path = ws.repo()
    ws.git("remote", "add", "origin", "ssh://git@host/observed.git")
    ws.store.append(
        ws.person_event(
            ws.owner,
            "workspace",
            "settings.changed",
            set={"repos": {"proj": {"path": str(path), "remote": "https://host/declared", "default_branch": "absent"}}},
        ),
        log="workspace",
    )
    claimed(cli)
    assert cli("set", "1", LINKS).code == 0
    assert cli("show", "1").code == 0
    assert "branch.pushed" not in types(ws)
    from orch.store import observe

    problems = []
    store = ws.other()
    try:
        assert observe.observe(store, "1", problems) == []
    finally:
        store.close()
    assert len(problems) == 1 and "repo.remote_mismatch" in problems[0]
    result = cli("submit")
    assert result.code != 0 and "observe.unavailable" in result.out + result.err
    ws.git("remote", "set-url", "origin", "ssh://git@HOST/DECLARED.git")
    assert cli("show", "1").code == 0
    event = next(e for e in ws.events("1") if e["type"] == "branch.pushed")
    assert event["repo_id"] == "https://host/declared"
    assert event["ref"] == "refs/heads/feat/x"

    # Even an unchanged commit must not bypass a later identity mismatch.
    ws.git("remote", "set-url", "origin", "https://host/other.git")
    problems = []
    store = ws.other()
    try:
        assert observe.observe(store, "1", problems) == []
    finally:
        store.close()
    assert len(problems) == 1 and "repo.remote_mismatch" in problems[0]
    assert len([e for e in ws.events("1") if e["type"] == "branch.pushed"]) == 1


def test_observe_records_new_push_after_remote_case_change(ws, cli):
    path = ws.repo()
    ws.git("remote", "add", "origin", "https://host/Declared.git")
    claimed(cli)
    assert cli("set", "1", LINKS).code == 0
    assert cli("show", "1").code == 0
    first = next(e for e in ws.events("1") if e["type"] == "branch.pushed")
    # Pin the same identity after its first observation, retaining the original entry's case.
    ws.store.append(
        ws.person_event(
            ws.owner,
            "workspace",
            "settings.changed",
            set={"repos": {"proj": {"path": str(path), "remote": "https://host/declared"}}},
        ),
        log="workspace",
    )
    ws.git("remote", "set-url", "origin", "https://HOST/DECLARED.git")
    (path / "impl.txt").write_text("changed\n")
    ws.git("commit", "-qam", "two")
    head = ws.git("rev-parse", "HEAD")
    assert cli("show", "1").code == 0
    events = [e for e in ws.events("1") if e["type"] == "branch.pushed"]
    assert len(events) == 2
    assert events[-1]["before"] == {"repo_id": first["repo_id"], "ref": first["ref"], "sha": first["sha"]}
    assert events[-1]["repo_id"] == "https://host/declared"
    assert events[-1]["sha"] == head
    assert ws.view("1").source_list[0]["sha"] == head
    assert cli("show", "1").code == 0
    assert len([e for e in ws.events("1") if e["type"] == "branch.pushed"]) == 2
