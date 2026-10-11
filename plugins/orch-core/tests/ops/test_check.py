"""``orch check``: the fast subset for hooks and CI, and ``--staged`` for a pre-commit hook."""

from __future__ import annotations

import subprocess

import pytest

from tests.ops.helpers import Cli
from tests.store.helpers import snapshot


@pytest.fixture
def repo(ws, cli):
    from orch.instructions import write_workspace_files

    for t in ("one", "two"):
        assert cli("new", t, "-m", f"text {t}").code == 0
    write_workspace_files(ws.root)

    def git(*a):
        return subprocess.run(["git", "-C", str(ws.root), *a], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    ws.git = git
    return ws


def check(ws, *args):
    return Cli(ws, grant=False, session=None).j("check", *args)


def staged(ws, *paths):
    ws.git("add", "-f", *paths)
    return check(ws, "--staged")


def codes(r):
    return [f["what"].split(":")[0] for f in r.data["findings"]]


def test_a_clean_workspace_passes_and_changes_nothing(repo):
    before = snapshot(repo.root)
    r = check(repo)
    assert r.code == 0 and r.data["problems"] == 0
    assert snapshot(repo.root) == before


def test_check_reports_a_hand_edit_without_healing_it(repo):
    p = repo.root / "tickets" / repo.uid("DEMO-0001") / "ticket.json"
    p.write_text(p.read_text().replace('"medium"', '"urgent"'))
    before = snapshot(repo.root)
    r = check(repo)
    assert r.code == 5 and codes(r) == ["projection.ticket"]
    assert snapshot(repo.root) == before  # no host event, no revert: a hook must not write


def test_check_reports_a_broken_chain(repo):
    p = repo.root / "tickets" / repo.uid("DEMO-0001") / "events.jsonl"
    p.write_bytes(p.read_bytes().replace(b"text one", b"text ONE").replace(b'"one"', b'"ONE"'))
    r = check(repo)
    assert r.code == 5 and codes(r)[0] == "chain.broken"


def test_a_clean_commit_passes(repo):
    r = staged(repo, "config.json", "keys.jsonl", "events", "tickets", "AGENTS.orch.md", ".claude")
    assert r.code == 0, r.out


def test_a_hand_edited_ticket_file_cannot_be_committed(repo):
    p = repo.root / "tickets" / repo.uid("DEMO-0001") / "body.md"
    p.write_text(p.read_text() + "\n## Plan\n\nforged plan\n")
    r = staged(repo, "tickets")
    assert "commit.edited" in codes(r) and r.code == 5


def test_a_forged_log_line_cannot_be_committed(repo):
    p = repo.root / "tickets" / repo.uid("DEMO-0001") / "events.jsonl"
    good = p.read_bytes()
    p.write_bytes(good + good.splitlines()[0] + b"\n")  # a line that was never appended by the host
    r = staged(repo, "tickets")
    assert codes(r)[0] == "chain.broken"  # the working copy is already broken: the hook says so
    p.write_bytes(good)  # the file is put back, but the forged line is what is staged
    r = check(repo, "--staged")
    assert "commit.forged" in codes(r) and r.code == 5


def test_an_older_part_of_the_log_may_be_committed(repo, cli):
    uid = repo.uid("DEMO-0001")
    repo.git("add", "-f", "config.json", "keys.jsonl", "events", "tickets")
    repo.git("commit", "-qm", "base")
    assert cli("claim", "DEMO-0001").code == 0
    p = repo.root / "tickets" / uid / "events.jsonl"
    repo.git("add", "-f", f"tickets/{uid}/events.jsonl")
    assert check(repo, "--staged").code == 0  # the whole, verified log
    assert len(p.read_bytes().splitlines()) == 3


def test_state_keys_and_grant_secrets_cannot_be_committed(repo):
    (repo.root / ".state" / "sessions").mkdir(parents=True, exist_ok=True)
    (repo.root / ".state" / "sessions" / "s.json").write_text("{}")
    (repo.root / "dk.key.json").write_text("{}")
    (repo.root / "notes.txt").write_text(f"export ORCH_GRANT={repo.grant}\n")
    r = staged(repo, ".state/sessions/s.json", "dk.key.json", "notes.txt")
    assert sorted(codes(r)) == ["commit.secret", "commit.secret", "commit.state"]


def test_stale_instructions_still_fail_the_check(repo):
    (repo.root / "AGENTS.orch.md").unlink()
    r = check(repo)
    assert r.code == 5 and r.data["findings"][0]["what"] == "missing"
