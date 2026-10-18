"""``orch migrate`` (M2): direct mode, an interrupted migration found by identity, the agent refusal, the journal's
root binding, a journal write failure and the direct-open completion."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from orch.cli.main import main
from orch.ops.errors import OrchError
from orch.store import migrate as mg
from tests.store.helpers import WS
from tests.store.test_migrate import Boom, _world


def _cli(env, *args: str, grant: bool = False, cwd: Path | None = None, monkeypatch=None, **extra: str):
    e = {"ORCH_STATE_DIR": str(env.host_state), "HOME": str(env.tmp), **extra}
    if cwd is None:
        e["ORCH_WORKSPACE"] = str(env.root)
    else:
        monkeypatch.chdir(cwd)
    if grant:
        e["ORCH_GRANT"] = "gr_x"
    out = io.StringIO()
    code = main(["migrate", *args], env=e, stdout=out, stderr=io.StringIO())
    return code, out.getvalue()


def test_direct_mode_migrates_and_a_second_run_says_nothing_to_do(tmp_path: Path) -> None:
    env = _world(tmp_path)
    code, text = _cli(env, "--json")
    assert code == 0 and json.loads(text) == {"ok": True, "migrated": True, "via": "direct"}
    assert (env.root / ".orch" / "config.json").is_file() and not (env.root / "config.json").exists()
    code, text = _cli(env)
    assert code == 0 and "nothing to do" in text


def test_an_agent_is_refused(tmp_path: Path) -> None:
    env = _world(tmp_path)
    code, text = _cli(env, grant=True)
    assert code == 3 and "op.not_allowed" in text and (env.root / "config.json").is_file()


def _git(root: Path, *argv: str, message: str | None = None) -> None:
    import subprocess

    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(root.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    cmd = ["git", "-c", "core.hooksPath=/dev/null", *argv]
    subprocess.run(
        cmd, cwd=root, env=base, check=True, capture_output=True, input=message.encode() if message else None
    )


def _records_repo(root: Path, wid: str) -> None:
    """A dedicated records repository at ``root`` as ``orch records commit`` leaves it: one commit with the exact
    message shape and the trailer for ``wid``."""
    _git(root, "init", "-q", "-b", "main", "--template=")
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text(".state/\naddons/\n")
    _git(root, "add", "config.json")
    msg = f"orch: records ws_seq 1\n\nOrch-Manifest: m_{'0' * 64}\nOrch-Records: {wid}\n"
    _git(root, "commit", "-q", "-F", "-", message=msg)


def test_the_records_repository_moves_with_the_store_and_a_projects_own_git_does_not(tmp_path: Path) -> None:
    from orch.records.tool import repo_is_ours

    env = _world(tmp_path)
    assert repo_is_ours(env.root, WS) is False  # no .git
    _records_repo(env.root, WS)
    assert repo_is_ours(env.root, WS) is True and repo_is_ours(env.root, "f" * 32) is False
    from orch.host import records

    records._write_record(
        env.host_state, WS, os.getuid(), {"v": 1, "private_remote": None, "exports": [], "repo": {"state": "ok"}}
    )
    head = (env.root / ".git" / "HEAD").read_text()
    code, text = _cli(env, "--json")
    assert code == 0, text
    assert (env.root / ".orch" / ".git" / "HEAD").read_text() == head and not (env.root / ".git").exists()
    assert repo_is_ours(env.root / ".orch", WS) is True  # the same repository, now beside the records it tracks
    # the project's own repository (another message) stays where it is
    env2 = _world(tmp_path / "b")
    _git(env2.root, "init", "-q", "-b", "main", "--template=")
    _git(env2.root, "add", "README.md")
    _git(env2.root, "commit", "-q", "-m", "the project's own first commit")
    assert repo_is_ours(env2.root, WS) is False
    code, text = _cli(env2, "--json")
    assert code == 0, text
    assert (env2.root / ".git" / "HEAD").is_file() and not (env2.root / ".orch" / ".git").exists()


def test_records_enabled_with_an_unidentifiable_repository_is_refused(tmp_path: Path) -> None:
    from orch.host import records

    env = _world(tmp_path)
    (env.root / ".git").mkdir()  # not a repository git's layout accepts
    records._write_record(
        env.host_state, WS, os.getuid(), {"v": 1, "private_remote": None, "exports": [], "repo": {"state": "ok"}}
    )
    code, text = _cli(env)
    assert code == 3 and "migrate.refused" in text and (env.root / "config.json").is_file()
    # not enabled: the same odd .git is the project's and is left alone
    env2 = _world(tmp_path / "b")
    (env2.root / ".git").mkdir()
    assert _cli(env2)[0] == 0 and (env2.root / ".git").is_dir()


def test_the_records_tool_resolves_the_store_dir_after_the_move(tmp_path: Path, monkeypatch) -> None:
    from orch.records.host_link import resolve

    env = _world(tmp_path)
    _cli(env)
    monkeypatch.chdir(env.root)
    e = {"ORCH_STATE_DIR": str(env.host_state), "HOME": str(env.tmp)}
    r = resolve(e, None, state=env.host_state)
    try:
        assert r.root == env.root.resolve() and r.record_root == env.root.resolve() / ".orch"
    finally:
        r.close()
    legacy = _world(tmp_path / "old")
    monkeypatch.chdir(legacy.root)
    r = resolve({"ORCH_STATE_DIR": str(legacy.host_state), "HOME": str(legacy.tmp)}, None, state=legacy.host_state)
    try:
        assert r.record_root == r.root
    finally:
        r.close()


def test_no_workspace_here(tmp_path: Path, monkeypatch) -> None:
    env = _world(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    code, text = _cli(env, cwd=empty, monkeypatch=monkeypatch, ORCH_WORKSPACE="")
    assert code == 2 and "not_found" in text


def test_an_interrupted_migration_is_found_by_identity_and_finished(tmp_path: Path, monkeypatch) -> None:
    env = _world(tmp_path)
    real = mg._rollback
    monkeypatch.setattr(mg, "_rollback", lambda *a, **k: None)  # the process died

    def fault(p: str) -> None:
        if p == "moved:events":
            raise Boom(p)

    with pytest.raises(Boom):
        mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS, fault=fault)
    monkeypatch.setattr(mg, "_rollback", real)
    assert not (env.root / "config.json").exists() or (env.root / mg.TMP_DIR).is_dir()
    sub = env.root / mg.TMP_DIR  # half way: discovery cannot see a store from inside the temp directory
    monkeypatch.undo()
    code, text = _cli(env, "--json", cwd=env.root, monkeypatch=monkeypatch, ORCH_WORKSPACE="")
    assert code == 0, text
    assert (env.root / "config.json").is_file() or (env.root / ".orch" / "config.json").is_file()
    assert not sub.exists()


def test_a_journal_of_another_project_directory_is_not_this_roots(tmp_path: Path) -> None:
    env = _world(tmp_path)
    jp = env.host_state / "hosts" / WS / mg.JOURNAL
    mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS)
    doc = json.loads(jp.read_text())
    doc.update(step="moving", root=[1, 2])  # a non-terminal journal that names some other directory
    jp.write_text(json.dumps(doc))
    os.chmod(jp, 0o600)
    assert mg.journal_step(env.host_state, WS, env.root) is None
    assert mg.finish_interrupted(env.root, state_dir=env.host_state, workspace_id=WS) == "none"
    assert mg.journal_step(env.host_state, WS) == "moving"  # untouched


def test_a_journal_write_failure_is_stuck_not_an_oserror(tmp_path: Path, monkeypatch) -> None:
    env = _world(tmp_path)
    before = sorted(p.name for p in env.root.iterdir())

    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mg, "write_atomic", broken)
    with pytest.raises(mg.MigrateError) as exc:
        mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS)
    assert exc.value.code == "migrate.stuck"
    assert sorted(p.name for p in env.root.iterdir()) == before  # nothing moved


def test_a_direct_command_over_an_unfinished_move_finishes_it_then_asks_again(tmp_path: Path, monkeypatch) -> None:
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    env = _world(tmp_path)

    def fault(p: str) -> None:
        if p == "committed":
            raise KeyboardInterrupt(p)

    with pytest.raises(KeyboardInterrupt):
        mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS, fault=fault)
    assert mg.journal_step(env.host_state, WS) == "committed"
    e = {"ORCH_WORKSPACE": str(env.root), "ORCH_STATE_DIR": str(env.host_state), "HOME": str(env.tmp)}
    ws = Workspace(e, files=Files.direct())
    with pytest.raises(OrchError) as exc:
        ws.store  # noqa: B018
    assert exc.value.code == "retry.later" and "finished" in exc.value.message
    assert mg.journal_step(env.host_state, WS) == "done"
    assert Workspace(e, files=Files.direct()).store is not None


def test_a_journal_naming_an_entry_the_migration_never_moves_is_stuck(tmp_path: Path) -> None:
    env = _world(tmp_path)
    mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS)
    jp = env.host_state / "hosts" / WS / mg.JOURNAL
    doc = json.loads(jp.read_text())
    doc.update(step="moving", names={**doc["names"], "../etc": [1, 2]})
    jp.write_text(json.dumps(doc))
    os.chmod(jp, 0o600)
    with pytest.raises(mg.MigrateError) as exc:
        mg.finish_interrupted(env.root, state_dir=env.host_state, workspace_id=WS)
    assert exc.value.code == "migrate.stuck"
