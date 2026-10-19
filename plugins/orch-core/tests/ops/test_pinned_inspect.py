"""``check``/``doctor`` and the workspace display name read the workspace only through the pinned root (#516): a
directory swapped for a symlink after the store was opened, or a root swapped after discovery, is never followed."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from orch.ops import health
from orch.ops.caps import Capabilities
from orch.store import StoreError
from tests.store.helpers import WS, Env
from tests.store.outside import snapshot, swap, watch


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.store is not None:
        e.store.close()


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    return d


def _ident(p):
    st = os.stat(p)
    return (st.st_dev, st.st_ino)


def test_inspect_refuses_a_root_swapped_after_discovery(env, outside, tmp_path):
    env.bootstrap()
    env.new_ticket()
    env.store.close()
    env.store = None
    ident = _ident(env.root)
    shutil.copytree(env.root, outside / "ws")
    before = snapshot(outside)
    env.root.rename(tmp_path / "ws-moved")
    os.symlink(outside / "ws", env.root)
    with watch(outside) as seen:
        found, s = health.inspect(env.root, WS, env.host_state, fast=False, caps=Capabilities.direct(), ident=ident)
    assert s is None
    assert [f.code for f in found if f.where == "workspace"] == ["validation.path"]
    # the one event naming the link is the root's own O_NOFOLLOW open, which refuses it (ELOOP): nothing below it
    assert [x for x in seen if not x.startswith(f"open {env.root} ->")] == []
    assert snapshot(outside) == before


def test_inspect_never_follows_a_directory_swapped_after_open(env, outside, tmp_path, monkeypatch):
    env.bootstrap()
    uid = env.new_ticket()
    env.store.checkpoint()
    env.store.close()
    env.store = None
    shutil.copytree(env.store_dir / "tickets" / uid, outside / uid)
    shutil.copytree(env.store_dir / ".state" / "checkpoints", outside / "checkpoints")
    real = health.InspectStore.open.__func__

    def opened(cls, *a, **k):
        s = real(cls, *a, **k)  # the store is open: now the swaps land
        swap(env.store_dir / "tickets" / uid, outside / uid, tmp_path / "t-moved")
        swap(env.store_dir / ".state" / "checkpoints", outside / "checkpoints", tmp_path / "cp-moved")
        return s

    monkeypatch.setattr(health.InspectStore, "open", classmethod(opened))
    before = snapshot(outside)
    with watch(outside) as seen:
        found, s = health.inspect(
            env.root, WS, env.host_state, fast=False, caps=Capabilities.direct(), ident=_ident(env.root)
        )
    if s is not None:
        s.close()
    assert seen == []
    assert snapshot(outside) == before
    assert found  # the swapped files are findings (missing), not outside reads


def test_the_workspace_name_is_read_below_the_pinned_root(env, outside, tmp_path):
    from orch.host import reads_ws

    s = env.bootstrap()
    shutil.copytree(env.root, outside / "ws")
    cfg = outside / "ws" / ".orch" / "config.json"
    cfg.write_bytes(cfg.read_bytes().replace(b'"Acme"', b'"Evil"'))
    assert reads_ws._name(SimpleNamespace(store=s), "X") == "Acme"
    env.root.rename(tmp_path / "ws-moved")
    os.symlink(outside / "ws", env.root)
    with watch(outside) as seen:
        assert reads_ws._name(SimpleNamespace(store=s), "X") == "Acme"
    assert seen == []
    s.close()


def test_a_records_snapshot_reads_nothing_through_a_swapped_project_path(env, outside, tmp_path):
    from orch.host import records as host_records

    s = env.bootstrap()
    uid = env.new_ticket()
    ctx = SimpleNamespace(store=s, holder=SimpleNamespace(), workspace_id=WS)
    want = host_records._take(ctx)
    shutil.copytree(env.root, outside / "ws")
    (outside / "ws" / ".orch" / "tickets" / uid / "ticket.json").write_bytes(b'{"forged": true}\n')
    env.root.rename(tmp_path / "ws-moved")
    os.symlink(outside / "ws", env.root)
    with watch(outside) as seen:
        with pytest.raises(StoreError):  # the project path now names another directory: the snapshot fails closed
            host_records._take(SimpleNamespace(store=s, holder=SimpleNamespace(), workspace_id=WS))
    assert all(x.endswith("/outside/ws") for x in seen)  # only the refused no-follow open of the project path itself
    assert want["files"]
    s.close()
