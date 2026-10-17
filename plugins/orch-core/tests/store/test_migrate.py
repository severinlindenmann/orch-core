"""The store migration engine (design B11-P2 M1): the happy path, every refusal, and a fault injected after every step
must converge to either the untouched legacy layout or the complete new one, with the logs unchanged and nothing
deleted."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from orch.store import Store
from orch.store import migrate as mg
from tests.store.helpers import WS, Env


class Boom(Exception):
    pass


def _tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root):
        for n in files:
            p = Path(dirpath) / n
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "link"
        for n in dirs:
            out[str(Path(dirpath, n).relative_to(root)) + "/"] = ""
    return out


def _world(tmp_path: Path):
    env = Env(tmp_path)
    s = env.bootstrap()
    env.new_ticket()
    s.close()
    env.store = None
    (env.host_state / "hosts" / WS).mkdir(parents=True, exist_ok=True)
    (env.root / "README.md").write_text("the person's own file")  # a project file that must stay
    return env


def _run(env: Env, **kw):
    return mg.migrate(env.root, state_dir=env.host_state, workspace_id=WS, lock_timeout=1.0, **kw)


def _legacy_names(root: Path) -> set[str]:
    return {n for n in mg.MOVE_NAMES if os.path.lexists(root / n)}


def test_the_store_moves_into_dot_orch_and_replays(tmp_path: Path) -> None:
    env = _world(tmp_path)
    before = _tree(env.root)
    out = _run(env)
    assert out["migrated"] is True
    assert _legacy_names(env.root) == set()
    assert (env.root / "README.md").read_text() == "the person's own file"
    assert sorted(p.name for p in env.root.iterdir()) == [".gitignore", ".orch", "README.md"]
    assert ".orch/" in (env.root / ".gitignore").read_text().split("\n")
    moved = {k: v for k, v in _tree(env.root / ".orch").items()}
    assert moved == {k: v for k, v in before.items() if k != "README.md" and not k.endswith("host-served")}
    assert oct(os.stat(env.root / ".orch").st_mode & 0o777) == "0o700"
    s = Store.open(env.root, expected_workspace_id=WS, load="all")
    try:
        assert not s.layout.legacy and s.state is not None
    finally:
        s.close()
    journal = json.loads((env.host_state / "hosts" / WS / mg.JOURNAL).read_text())
    assert journal["step"] == "done"
    assert oct(os.stat(env.host_state / "hosts" / WS / mg.JOURNAL).st_mode & 0o777) == "0o600"


def test_a_second_run_is_already_migrated(tmp_path: Path) -> None:
    env = _world(tmp_path)
    _run(env)
    again = _run(env)
    assert again["migrated"] is False


def test_the_gitignore_line_is_added_once_and_user_lines_are_kept(tmp_path: Path) -> None:
    env = _world(tmp_path)
    (env.root / ".gitignore").write_text("node_modules/\n.state/")
    _run(env)
    assert (env.root / ".gitignore").read_text() == "node_modules/\n.state/\n.orch/\n"


@pytest.mark.parametrize("what", ["double", "link", "tmp", "no_config", "odd"])
def test_refusals_move_nothing(tmp_path: Path, what: str) -> None:
    env = _world(tmp_path)
    if what == "double":
        (env.root / ".orch").mkdir()
        (env.root / ".orch" / "config.json").write_text(
            json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})
        )
        code = "migrate.layout"
    elif what == "link":
        (env.root / "addons").symlink_to(env.root / "events")
        code = "migrate.unsafe"
    elif what == "tmp":
        (env.root / mg.TMP_DIR).mkdir()
        code = "migrate.layout"
    elif what == "no_config":
        (env.root / "config.json").unlink()
        code = "migrate.layout"
    else:
        (env.root / "keys.jsonl").unlink(missing_ok=True)
        (env.root / "keys.jsonl").mkdir()  # a directory where a file belongs
        code = "migrate.unsafe"
    before = _tree(env.root)
    with pytest.raises(mg.MigrateError) as exc:
        _run(env)
    assert exc.value.code == code
    assert _tree(env.root) == before or what == "double"
    assert _legacy_names(env.root) >= {"events", "tickets", ".state"}


def test_a_held_store_lock_refuses_busy_and_changes_nothing(tmp_path: Path) -> None:
    from orch.store.lock import FileLock

    env = _world(tmp_path)
    before = _tree(env.root)
    lock = FileLock(env.root / ".state" / "lock", timeout=1.0)
    import threading

    box: list[object] = []

    def other() -> None:  # another thread is another holder for FileLock (re-entrancy is per thread)
        try:
            _run(env)
        except mg.MigrateError as exc:
            box.append(exc)

    with lock:
        t = threading.Thread(target=other)
        t.start()
        t.join(10)
    assert box and box[0].code == "migrate.busy"  # type: ignore[attr-defined]
    assert _tree(env.root) == before


# every point at which the engine can be interrupted
def _points(env: Env) -> list[str]:
    seen: list[str] = []
    _run(env, fault=seen.append)
    return seen


@pytest.mark.parametrize("crash", [False, True])
def test_fault_injection_converges_after_every_step(tmp_path: Path, monkeypatch, crash: bool) -> None:
    """``crash``: the process dies at the point (the in-process rollback never runs); else the exception path runs."""
    real_rollback = mg._rollback
    probe = _world(tmp_path / "probe")
    points = _points(probe)
    assert points[0] == "planned" and points[-1] == "done" and "committed" in points and "renamed" in points
    assert any(p.startswith("moved:") for p in points)
    for i, point in enumerate(points):
        env = _world(tmp_path / f"w{i}")
        before = _tree(env.root)
        logs = {k: v for k, v in before.items() if k.endswith("events.jsonl") or k.endswith("workspace.jsonl")}

        def fault(p: str, point: str = point) -> None:
            if p == point:
                raise Boom(p)

        if crash:
            monkeypatch.setattr(mg, "_rollback", lambda *a, **k: None)
        with pytest.raises((Boom, mg.MigrateError)):
            _run(env, fault=fault)
        monkeypatch.setattr(mg, "_rollback", real_rollback)
        # whatever the crash left, recovery converges
        step = mg.finish_interrupted(env.root, state_dir=env.host_state, workspace_id=WS)
        committed = points.index("committed")
        if i < committed:
            assert step == "rolled_back", (point, step)
            assert _tree(env.root) == before, point  # untouched legacy, no tmp, nothing deleted
            assert not os.path.lexists(env.root / mg.TMP_DIR) and not os.path.lexists(env.root / ".orch")
        else:
            assert step in ("done", "none"), (point, step)
            assert _legacy_names(env.root) == set()
            after = _tree(env.root / ".orch")
            assert {k: v for k, v in after.items() if k in logs} == logs
        # and a fresh run reaches the new layout from either
        _run(env)
        assert _legacy_names(env.root) == set() and (env.root / ".orch" / "config.json").is_file()
        assert {k: v for k, v in _tree(env.root / ".orch").items() if k in logs} == logs


def test_a_verification_failure_rolls_back_and_says_so(tmp_path: Path, monkeypatch) -> None:
    env = _world(tmp_path)
    before = _tree(env.root)
    real = mg._digests
    calls = {"n": 0}

    def lying(fd: int):
        calls["n"] += 1
        out = real(fd)
        return {**out, "events/workspace.jsonl": "bad"} if calls["n"] == 2 else out

    monkeypatch.setattr(mg, "_digests", lying)
    with pytest.raises(mg.MigrateError) as exc:
        _run(env)
    assert exc.value.code == "migrate.verify"
    assert _tree(env.root) == before and not os.path.lexists(env.root / ".orch")


def test_rollback_refuses_to_move_back_what_is_not_the_recorded_entry(tmp_path: Path, monkeypatch) -> None:
    env = _world(tmp_path)
    real = mg._rollback
    monkeypatch.setattr(mg, "_rollback", lambda *a, **k: None)  # a crash: the process never got to roll back

    def fault(p: str) -> None:
        if p == "moved:events":
            raise Boom(p)

    with pytest.raises(Boom):
        _run(env, fault=fault)
    monkeypatch.setattr(mg, "_rollback", real)
    tmp = env.root / mg.TMP_DIR
    (tmp / "events").rename(tmp / "events-away")
    (tmp / "events").mkdir()  # another inode under the recorded name
    with pytest.raises(mg.MigrateError) as exc:
        mg.finish_interrupted(env.root, state_dir=env.host_state, workspace_id=WS)
    assert exc.value.code == "migrate.stuck"
    assert (tmp / "events-away").is_dir()  # nothing was deleted or guessed


def test_the_ongoing_migration_is_a_journal_other_runs_do_not_overwrite(tmp_path: Path) -> None:
    env = _world(tmp_path)
    jp = env.host_state / "hosts" / WS / mg.JOURNAL
    jp.write_text("{broken")
    os.chmod(jp, 0o600)
    with pytest.raises(mg.MigrateError) as exc:
        _run(env)
    assert exc.value.code == "migrate.stuck"
    os.chmod(jp, 0o644)
    with pytest.raises(mg.MigrateError):
        _run(env)
