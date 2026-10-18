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


def _commit(root: Path, wid: str | None, seq: int, add: str = "config.json") -> str:
    """One commit in ``root``: a records commit of ``wid`` (the exact message shape), or a plain code commit."""
    import subprocess

    _git(root, "add", add)
    shape = f"orch: records ws_seq {seq}\n\nOrch-Manifest: m_{'0' * 64}\nOrch-Records: {wid}\n"
    _git(root, "commit", "-q", "--allow-empty", "-F", "-", message=shape if wid else f"code {seq}\n")
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, env={"PATH": os.environ["PATH"]}
    )
    return out.stdout.decode().strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main", "--template=")


def _record(env, head: str | None, *, configured: bool = True) -> None:
    from orch.host import records

    doc: dict = {"v": 1, "private_remote": None, "exports": []}
    if configured:
        doc["repo"] = {"state": "ok"}
    if head:
        doc["lasts"] = {"p_" + "0" * 32: {"commit": head}}
    records._write_record(env.host_state, WS, os.getuid(), doc)


def test_the_records_repository_moves_only_when_the_host_recorded_its_head(tmp_path: Path) -> None:
    env = _world(tmp_path)
    _init_repo(env.root)
    head = _commit(env.root, WS, 1)
    _record(env, head)
    git_head = (env.root / ".git" / "HEAD").read_text()
    code, text = _cli(env, "--json")
    assert code == 0, text
    assert (env.root / ".orch" / ".git" / "HEAD").read_text() == git_head and not (env.root / ".git").exists()


def test_a_forged_trailer_on_a_code_repository_is_not_moved(tmp_path: Path) -> None:
    env = _world(tmp_path)
    _init_repo(env.root)
    _commit(env.root, None, 1)  # the person's real code history
    _commit(env.root, WS, 2)  # an agent's commit with a perfect records message on top
    _record(env, None)  # records configured, but the host never recorded this commit
    code, text = _cli(env, "--json")
    assert code == 0, text
    assert (env.root / ".git" / "HEAD").is_file() and not (env.root / ".orch" / ".git").exists()
    # even a host-recorded HEAD does not help when an older commit is not a records commit
    env2 = _world(tmp_path / "b")
    _init_repo(env2.root)
    _commit(env2.root, None, 1)
    tip = _commit(env2.root, WS, 2)
    _record(env2, tip)
    code, text = _cli(env2, "--json")
    assert code == 0, text
    assert (env2.root / ".git").is_dir() and not (env2.root / ".orch" / ".git").exists()


def test_a_repository_is_never_moved_when_records_are_not_configured(tmp_path: Path) -> None:
    env = _world(tmp_path)
    _init_repo(env.root)
    head = _commit(env.root, WS, 1)
    _record(env, head, configured=False)  # a perfect records history, recorded, but records are not enabled
    code, text = _cli(env, "--json")
    assert code == 0, text
    assert (env.root / ".git").is_dir() and not (env.root / ".orch" / ".git").exists()


def test_records_enabled_with_an_unidentifiable_repository_is_refused(tmp_path: Path) -> None:
    env = _world(tmp_path)
    (env.root / ".git").mkdir()  # not a repository git's layout accepts
    _record(env, None)
    code, text = _cli(env)
    assert code == 3 and "migrate.refused" in text and (env.root / "config.json").is_file()
    env2 = _world(tmp_path / "b")  # not enabled: the same odd .git is the project's and is left alone
    (env2.root / ".git").mkdir()
    assert _cli(env2)[0] == 0 and (env2.root / ".git").is_dir()


def test_a_git_directory_swapped_after_identification_is_refused_untouched(tmp_path: Path) -> None:
    env = _world(tmp_path)
    _init_repo(env.root)
    _commit(env.root, WS, 1)
    st = os.stat(env.root / ".git")
    (env.root / ".git").rename(env.root / "git-away")
    (env.root / ".git").mkdir()  # another directory under the name, another inode
    before = sorted(p.name for p in env.root.iterdir())
    with pytest.raises(mg.MigrateError) as exc:
        mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS, move_git=(st.st_dev, st.st_ino))
    assert exc.value.code == "migrate.unsafe" and sorted(p.name for p in env.root.iterdir()) == before


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


def test_records_init_is_refused_while_a_store_move_is_under_way(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from orch.records.cli import RecordsError, _no_store_move

    env = _world(tmp_path)
    r = SimpleNamespace(root=env.root, state=env.host_state, workspace_id=WS, record_root=env.root)
    _no_store_move(r)  # quiet legacy
    (env.root / mg.TMP_DIR).mkdir()
    with pytest.raises(RecordsError):
        _no_store_move(r)
    (env.root / mg.TMP_DIR).rmdir()
    r2 = SimpleNamespace(root=env.root, state=env.host_state, workspace_id=WS, record_root=env.root / ".orch")
    with pytest.raises(RecordsError):  # the record root is not the store as it is on disk
        _no_store_move(r2)


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
