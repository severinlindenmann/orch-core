"""Every store transaction re-checks the layout under the guard and the lock, so a stale opener fails closed when its
``.orch`` is swapped or gone, and the host guard still holds the ownership lock around it."""

from __future__ import annotations

from pathlib import Path

import pytest

from orch.host.ownership import DirectGuard, OwnershipLock
from orch.store import StoreError
from tests.store.helpers import WS, Env


def test_a_dot_orch_opener_fails_closed_when_the_store_directory_is_replaced(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = env.open()
    assert s.layout.store == env.root / ".orch"
    with s.locked():
        pass  # unchanged: fine
    (env.root / ".orch").rename(tmp_path / "moved-away")
    (env.root / ".orch").mkdir()
    with pytest.raises(StoreError, match="no longer the one that was opened|gone|layout"):
        with s.locked():
            pass
    s.close()


def test_a_dot_orch_opener_fails_closed_when_the_store_directory_is_gone(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = env.open()
    (env.root / ".orch").rename(tmp_path / "moved-away")
    with pytest.raises(StoreError, match="run the command again"):
        with s.locked():
            pass
    s.close()


def test_a_running_host_is_told_to_restart_not_to_run_the_command_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = env.open()
    s.before_append = lambda: None  # what the host sets on the store it serves
    (env.root / ".orch").rename(tmp_path / "moved-away")
    with pytest.raises(StoreError, match="restart the host: orch host restart") as caught:
        with s.locked():
            pass
    assert "run the command again" not in str(caught.value.detail)
    s.close()


def test_the_guard_runs_before_the_layout_check_and_waits_for_the_ownership_lock(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    guard = DirectGuard(env.host_state, WS, timeout=0.2)
    s = env.open(guard=guard)
    with OwnershipLock(env.host_state, WS).exclusive(1):  # what a host holds while it takes the workspace over
        with pytest.raises(StoreError) as caught:
            with s.locked():
                pass
        assert caught.value.code == "store.busy"  # the layout is not even looked at while the lock is held
    with s.locked():
        pass
    s.close()


def test_a_new_workspace_without_a_config_yet_keeps_its_opened_layout(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = env.open()  # no genesis yet: nothing resolves; this is not a layout change
    with s.locked():
        pass
    s.close()


def test_an_opener_with_a_genesis_refuses_when_config_json_is_deleted_or_invalid(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    cfg = env.store_dir / "config.json"
    good = cfg.read_bytes()
    for mutate in (
        lambda: cfg.unlink(),
        lambda: cfg.write_text("{not json"),
        lambda: cfg.write_bytes(b"x" * (2 << 20)),
    ):
        cfg.write_bytes(good)
        s = env.open()
        mutate()
        with pytest.raises(StoreError, match="config.json is gone or unreadable"):
            with s.locked():
                pass
        s.close()
        cfg.unlink(missing_ok=True)
    cfg.write_bytes(good)


def test_a_stale_opener_never_recreates_state_in_a_replaced_store(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    s = env.open()
    (env.root / ".orch").rename(tmp_path / "moved-away")
    with pytest.raises(StoreError):
        with s.locked():
            pass
    assert not (env.root / ".orch").exists() and not (env.root / ".state").exists()  # nothing re-created
    s.close()


def test_a_dot_orch_swapped_between_the_resolve_and_the_open_fails_closed_and_makes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    from orch.store import store as store_mod

    env = Env(tmp_path)
    env.bootstrap().close()
    real = store_mod.open_store_fs

    def swapped_after_resolve(*a, **k):
        out = real(*a, **k)
        (env.root / ".orch").rename(tmp_path / "moved-away")  # another inode takes the name in the window
        (env.root / ".orch").mkdir(mode=0o700)
        return out

    monkeypatch.setattr(store_mod, "open_store_fs", swapped_after_resolve)
    with pytest.raises(StoreError) as caught:
        env.open()
    assert caught.value.code == "validation.path"
    assert sorted(p.name for p in (env.root / ".orch").iterdir()) == []  # events/tickets/.state were not made
