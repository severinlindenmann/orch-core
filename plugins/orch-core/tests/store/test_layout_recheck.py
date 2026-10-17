"""Step 7 (L): every store transaction re-checks the layout under the guard and the lock, so a stale opener fails closed
after a migrate (design B11-P2 §3) and the host guard still holds the ownership lock around it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orch import layout
from orch.host.ownership import DirectGuard, OwnershipLock
from orch.store import StoreError
from tests.store.helpers import WS, Env


def _move_into_dot_orch(root: Path) -> None:
    items = list(root.iterdir())
    (root / ".orch").mkdir(mode=0o700)
    for item in items:
        shutil.move(str(item), str(root / ".orch" / item.name))


def test_a_legacy_opener_fails_closed_after_the_store_moved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = env.bootstrap()
    s.state  # noqa: B018  (a read before the move works)
    s.close()
    s = env.open()
    _move_into_dot_orch(env.root)
    with pytest.raises(StoreError, match=r"layout changed|\.state is gone"):
        with s.locked():
            pass
    with pytest.raises(StoreError, match=r"layout changed|\.state is gone"):
        s.append(env.person_event(env.owner, "workspace", "settings.changed", set={"x": 1}), log="workspace")
    s.close()


def test_a_dot_orch_opener_fails_closed_when_the_store_directory_is_replaced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(layout, "WRITE_NEW", True)
    env = Env(tmp_path)
    env.root.mkdir(exist_ok=True)
    s = env.open(create=True)
    assert not s.layout.legacy
    with s.locked():
        pass  # unchanged: fine
    (env.root / ".orch").rename(tmp_path / "moved-away")
    (env.root / ".orch").mkdir()
    with pytest.raises(StoreError, match="no longer the one that was opened|gone|layout"):
        with s.locked():
            pass
    s.close()


def test_a_dot_orch_opener_fails_closed_when_the_store_directory_is_gone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(layout, "WRITE_NEW", True)
    env = Env(tmp_path)
    s = env.open(create=True)
    (env.root / ".orch").rename(tmp_path / "moved-away")
    with pytest.raises(StoreError):
        with s.locked():
            pass
    s.close()


def test_the_guard_runs_before_the_layout_check_and_waits_for_a_migrate(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    guard = DirectGuard(env.host_state, WS, timeout=0.2)
    s = env.open(guard=guard)
    with OwnershipLock(env.host_state, WS).exclusive(1):  # what a migrate holds from planning until done
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


def test_a_legacy_opener_with_a_genesis_refuses_when_config_json_is_deleted_or_invalid(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    cfg = env.root / "config.json"
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


def test_a_stale_legacy_opener_never_recreates_state_in_the_root(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.bootstrap().close()
    s = env.open()
    _move_into_dot_orch(env.root)
    with pytest.raises(StoreError):
        with s.locked():
            pass
    assert sorted(p.name for p in env.root.iterdir()) == [".orch"]  # no .state, no events, nothing re-created
    s.close()
