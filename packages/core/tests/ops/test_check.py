"""``orch check``: the fast subset for hooks and CI, and ``--staged`` for a pre-commit hook."""

from __future__ import annotations

import subprocess

import pytest

from orch.ops.caps import Capabilities
from tests.ops.helpers import Cli
from tests.store.helpers import snapshot


@pytest.fixture
def repo(ws, cli):
    from orch.instructions import write_workspace_files

    for t in ("one", "two"):
        assert cli("new", t, "-m", f"text {t}").code == 0
    write_workspace_files(ws.root, store=ws.store_dir)

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
    p = repo.store_dir / "tickets" / repo.uid("DEMO-0001") / "ticket.json"
    p.write_text(p.read_text().replace('"medium"', '"urgent"'))
    before = snapshot(repo.root)
    r = check(repo)
    assert r.code == 5 and codes(r) == ["projection.ticket"]
    assert snapshot(repo.root) == before  # no host event, no revert: a hook must not write


def test_check_reports_a_broken_chain(repo):
    p = repo.store_dir / "tickets" / repo.uid("DEMO-0001") / "events.jsonl"
    p.write_bytes(p.read_bytes().replace(b"text one", b"text ONE").replace(b'"one"', b'"ONE"'))
    r = check(repo)
    assert r.code == 5 and codes(r)[0] == "chain.broken"


def test_a_clean_commit_passes(repo):
    r = staged(
        repo,
        ".orch/config.json",
        ".orch/keys.jsonl",
        ".orch/events",
        ".orch/tickets",
        ".orch/AGENTS.orch.md",
        ".claude",
    )
    assert r.code == 0, r.out


def test_a_hand_edited_ticket_file_cannot_be_committed(repo):
    p = repo.store_dir / "tickets" / repo.uid("DEMO-0001") / "body.md"
    p.write_text(p.read_text() + "\n## Plan\n\nforged plan\n")
    r = staged(repo, ".orch/tickets")
    assert "commit.edited" in codes(r) and r.code == 5


def test_a_forged_log_line_cannot_be_committed(repo):
    p = repo.store_dir / "tickets" / repo.uid("DEMO-0001") / "events.jsonl"
    good = p.read_bytes()
    p.write_bytes(good + good.splitlines()[0] + b"\n")  # a line that was never appended by the host
    r = staged(repo, ".orch/tickets")
    assert codes(r)[0] == "chain.broken"  # the working copy is already broken: the hook says so
    p.write_bytes(good)  # the file is put back, but the forged line is what is staged
    r = check(repo, "--staged")
    assert "commit.forged" in codes(r) and r.code == 5


def test_an_older_part_of_the_log_may_be_committed(repo, cli):
    uid = repo.uid("DEMO-0001")
    repo.git("add", "-f", ".orch/config.json", ".orch/keys.jsonl", ".orch/events", ".orch/tickets")
    repo.git("commit", "-qm", "base")
    assert cli("claim", "DEMO-0001").code == 0
    p = repo.store_dir / "tickets" / uid / "events.jsonl"
    repo.git("add", "-f", f".orch/tickets/{uid}/events.jsonl")
    assert check(repo, "--staged").code == 0  # the whole, verified log
    assert len(p.read_bytes().splitlines()) == 3


def test_state_keys_and_grant_secrets_cannot_be_committed(repo):
    (repo.store_dir / ".state" / "sessions").mkdir(parents=True, exist_ok=True)
    (repo.store_dir / ".state" / "sessions" / "s.json").write_text("{}")
    (repo.root / "dk.key.json").write_text("{}")
    (repo.root / "notes.txt").write_text(f"export ORCH_GRANT={repo.grant}\n")
    r = staged(repo, ".orch/.state/sessions/s.json", "dk.key.json", "notes.txt")
    assert sorted(codes(r)) == ["commit.secret", "commit.secret", "commit.state"]


def test_stale_instructions_still_fail_the_check(repo):
    (repo.store_dir / "AGENTS.orch.md").unlink()
    r = check(repo)
    assert r.code == 5 and r.data["findings"][0]["what"] == "missing"


def test_a_type_change_to_a_symlink_and_a_deleted_log_cannot_be_committed(repo):
    import os

    uid = repo.uid("DEMO-0001")
    p = repo.store_dir / "tickets" / uid / "ticket.json"
    keep = p.read_bytes()
    p.unlink()
    os.symlink("/etc/passwd", p)
    repo.git("add", "-f", f".orch/tickets/{uid}/ticket.json")
    p.unlink()
    p.write_bytes(keep)  # the file is put back on disk, the link is what is staged
    r = check(repo, "--staged")
    assert "commit.link" in codes(r) and r.code == 5
    repo.git("reset", "-q")
    repo.git("add", "-f", ".orch/tickets")
    repo.git("commit", "-qm", "base")
    repo.git("rm", "--cached", "-q", f".orch/tickets/{uid}/events.jsonl")
    assert "commit.deleted" in codes(check(repo, "--staged"))


def test_a_failing_check_does_not_start_with_ok(repo):
    (repo.store_dir / "AGENTS.orch.md").unlink()
    r = Cli(repo, grant=False, session=None)("check")
    assert r.code == 5 and r.first.startswith("check: ") and not r.out.startswith("ok")


def test_unknown_files_and_a_deleted_artifact_cannot_be_committed(repo, cli):
    uid = repo.uid("DEMO-0001")
    (repo.store_dir / "events" / "other.jsonl").write_text("{}\n")
    (repo.store_dir / "tickets" / uid / "notes.txt").write_text("x")
    (repo.store_dir / "tickets" / "README").write_text("x")
    r = staged(repo, ".orch/events/other.jsonl", f".orch/tickets/{uid}/notes.txt", ".orch/tickets/README")
    assert r.code == 5 and codes(r) == ["commit.unknown"] * 3
    repo.git("reset", "-q")
    for p in (".orch/events/other.jsonl", f".orch/tickets/{uid}/notes.txt", ".orch/tickets/README"):
        (repo.root / p).unlink()
    src = repo.root.parent / "a.txt"
    src.write_text("artifact")
    assert cli("claim", "DEMO-0001").code == 0
    assert cli("artifact", "add", str(src)).code == 0, "artifact add"
    repo.git("add", "-f", ".orch/tickets", ".orch/events", ".orch/config.json", ".orch/keys.jsonl")
    assert check(repo, "--staged").code == 0
    repo.git("commit", "-qm", "base")
    repo.git("rm", "--cached", "-q", f".orch/tickets/{uid}/artifacts/a.txt")
    r = check(repo, "--staged")
    assert r.code == 5 and codes(r) == ["commit.deleted"]


@pytest.mark.parametrize("status", ["A", "D"])
@pytest.mark.parametrize(
    "rel",
    [
        "Tickets/{uid}/events.jsonl",
        "EVENTS/workspace.jsonl",
        ".State/keys/x",
        "tickets/{uid}/EVENTS.jsonl",
        "events/WORKSPACE.jsonl",
        "tickets/{uid}/artifacts/café.txt",
        "EVENTS/café.jsonl",
        ".STATE/kéys/x",
    ],
)
def test_staged_paths_are_guarded_without_case_or_ascii_aliases(ws, cli, monkeypatch, status, rel):
    cli("new", "one")
    rel = ".orch/" + rel.format(uid=ws.uid("1"))
    _staged_stub(ws, monkeypatch, status, rel)


@pytest.mark.parametrize("status", ["A", "D"])
@pytest.mark.parametrize("rel", [".ORCH/tickets/{uid}/events.jsonl", ".Orch/.state/x", ".orCH/config.json"])
def test_another_spelling_of_the_store_folder_is_refused_not_skipped(ws, cli, monkeypatch, status, rel):
    cli("new", "one")
    _staged_stub(ws, monkeypatch, status, rel.format(uid=ws.uid("1")))


@pytest.mark.parametrize("status", ["A", "D"])
@pytest.mark.parametrize(
    "rel",
    [".state/x", ".STATE/k\u00e9ys/x", "sub/.state/x", "sub/.STATE/x", ".orch/.state/x", "a/b/.State/c/d", ".state"],
)
def test_local_state_is_refused_in_any_folder_and_spelling(ws, cli, monkeypatch, status, rel):
    from orch.ops import health

    cli("new", "one")
    raw = f":100644 100644 {'0' * 40} {'1' * 40} {status}\0{rel}\0".encode()
    monkeypatch.setattr(health, "_git", lambda root, op, **kw: raw if op == "staged_raw" else b"innocent")
    monkeypatch.setattr(health, "_gate", lambda root, caps: None)
    store = ws.other()
    try:
        findings = health.check_staged(store, ws.root, Capabilities.direct())
        assert [f.code for f in findings] == ["commit.state"]
    finally:
        store.close()


def _staged_stub(ws, monkeypatch, status, rel):
    from orch.ops import health

    raw = f":100644 100644 {'0' * 40} {'1' * 40} {status}\0{rel}\0".encode()
    monkeypatch.setattr(health, "_git", lambda root, op, **kw: raw if op == "staged_raw" else b"innocent")
    monkeypatch.setattr(health, "_gate", lambda root, caps: None)  # git is stubbed out: no repository to gate (#546)
    store = ws.other()
    try:
        findings = health.check_staged(store, ws.root, Capabilities.direct())
        assert len(findings) == 1 and findings[0].code in ("commit.unknown", "commit.state")
    finally:
        store.close()
