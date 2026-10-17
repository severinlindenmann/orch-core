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


def test_records_enabled_is_refused_in_direct_mode_too(tmp_path: Path) -> None:
    from orch.host import records

    env = _world(tmp_path)
    records._write_record(
        env.host_state, WS, os.getuid(), {"v": 1, "private_remote": None, "exports": [], "repo": {"state": "ok"}}
    )
    code, text = _cli(env)
    assert code == 3 and "migrate.refused" in text and (env.root / "config.json").is_file()


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
