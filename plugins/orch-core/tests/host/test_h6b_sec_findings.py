"""H6b security review (Opus): adversarial tests for the dashboard reads. Each test asserts the behaviour the
contract (orch-v2-host.md §0.5, §0.8, §0.8.5) or the store's own rules require; a failing test is a finding."""

from __future__ import annotations

import json
import os
import threading
import types
from pathlib import Path

import pytest

from orch.host import pages as pages_mod
from orch.host import reads as _reads
from orch.host import sessions

_reads._load()  # register every read handler first: importing one reads_* module alone hides the others
from orch.host import reads_ws  # noqa: E402
from tests.host.dispatch_helpers import err  # noqa: E402
from tests.host.live_helpers import SESSION, world  # noqa: E402
from tests.host.live_helpers import ok as ok_  # noqa: E402


def ok(res):
    return ok_(res)["data"]


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


# --- F: a page token with a non-ASCII MAC must be page.expired, never an internal error ------------------------------


def test_page_token_non_ascii_mac_is_page_expired_unit():
    pages = pages_mod.Pages("ws_x")
    snap = pages.open("ticket.query", "uid:1", "e", None, {})
    token = pages.token(snap, 1, {})
    head, body, _ = token.split(".")
    bad = f"{head}.{body}.ééé"
    with pytest.raises(Exception) as info:
        pages.resume("ticket.query", bad, "uid:1", "e", None, {})
    assert getattr(info.value, "code", None) == "page.expired", repr(info.value)  # TypeError today


@pytest.mark.parametrize("op", ["ticket.query", "activity.list", "workspace.settings"])
def test_page_token_non_ascii_mac_is_page_expired_on_the_wire(w, op):
    c = w.client()
    assert err(c.req(op, {"page": "p1.eyJvIjoxfQ.é"})) == "page.expired"


# --- F: workspace.show reads the agent-writable config.json without the store's rules -------------------------------


def _ctx(root: Path):
    """A read context whose store has the real pinned root (#516/#564: the name is read below ``store.fs``)."""
    from orch.store.rootfs import RootFS

    return types.SimpleNamespace(store=types.SimpleNamespace(root=root, fs=RootFS.at(root)))


def test_workspace_name_does_not_follow_a_symlinked_config_json(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "elsewhere.json"
    secret.write_text(json.dumps({"workspace": {"name": "READ-THROUGH-A-SYMLINK"}}))
    (root / "config.json").symlink_to(secret)
    ctx = _ctx(root)
    assert reads_ws._name(ctx, "DEMO") == "DEMO"  # the pinned root never follows a link (RootFS.read_or_none)
    # the control: a real workspace's config.json in its place is read through the same pinned root
    from tests.store.helpers import Env

    env = Env(tmp_path / "control")
    env.bootstrap().close()
    (root / "config.json").unlink()
    (root / "config.json").write_bytes((env.store_dir / "config.json").read_bytes())
    assert reads_ws._name(ctx, "DEMO") == "Acme"


def test_workspace_name_survives_a_deeply_nested_config_json(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "config.json").write_text("[" * 200_000)
    assert reads_ws._name(_ctx(root), "DEMO") == "DEMO"  # RecursionError is not caught today


def test_workspace_name_does_not_block_on_a_fifo_config_json(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    fifo = root / "config.json"
    os.mkfifo(fifo)
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(reads_ws._name(_ctx(root), "DEMO")), daemon=True)
    t.start()
    t.join(1.0)
    blocked = t.is_alive()
    if blocked:  # release the reader so the test process does not keep a stuck thread
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(2.0)
    assert not blocked, "workspace.show blocks the workspace worker on a FIFO config.json"


# --- F: one grant can flood the host session records -----------------------------------------------------------------


def test_one_grant_cannot_evict_another_persons_session_records(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_RECORDS", 50)
    d = tmp_path / "hosts" / "ws"
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    os.chmod(d.parent, 0o700)
    flushes = []
    orig = sessions.Records._flush
    monkeypatch.setattr(sessions.Records, "_flush", lambda self: (flushes.append(1), orig(self))[1])
    rec = sessions.Records(d / "sessions.json", os.getuid())
    now = 1_000_000.0
    rec.note("s_01J9ZP00000000000000000001", "orch-cli", "p_victim", "gr_victim", now)
    for n in range(200):  # an agent picks a fresh s_<ULID> for every request
        rec.note(f"s_01J9ZP{n:020d}", "orch-cli", "p_attacker", "gr_attacker", now + 1 + n)
    left = {(r.grant_id, r.session) for r in rec.snapshot(now + 300)}
    assert ("gr_victim", "s_01J9ZP00000000000000000001") in left, "a single grant pushed out another's records"
    assert len(flushes) < 50, f"{len(flushes)} full-file rewrites for 201 requests"


# --- F: every first page keeps a snapshot of every row, even when nothing is paged -----------------------------------


def test_a_single_page_result_keeps_no_snapshot(w):
    c = w.client()
    for _ in range(5):
        data = ok(c.req("ticket.query", {}))
        assert data["next_page"] is None
    for _ in range(5):
        assert ok(c.req("activity.list", {"limit": 200}))["next_page"] is None
    pages = getattr(w.service, "pages", None)
    held = len(pages._snaps) if pages is not None else 0
    assert held == 0, f"{held} snapshots held for results that had no next page"


def test_grant_scoped_reads_still_answer(w):
    """Sanity: the reads above work for G too (the fixture is usable)."""
    assert ok(w.client().req("ticket.query", {}, grant=w.grant, session=SESSION))["total"] >= 0
