"""Child-process entry points for the lock and crash tests (importable by ``multiprocessing`` spawn)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from orch.custody import FileBackend
from orch.store import BackendSigner, Store

WS = "705d40abbb8c1c90354a1acaa94c935c"


T0 = 1_790_000_000  # the Env clock; children run on a frozen clock so that `at` has to bump across processes


def open_store(root: str, host_dir: str, host_state: str, **kw) -> Store:
    host = BackendSigner(FileBackend(host_dir), "wsk")
    kw.setdefault("clock", lambda: T0 + 5)
    kw.setdefault("load", "all")
    return Store.open(root, expected_workspace_id=WS, host=host, host_state_dir=host_state, **kw)


def append_notes(root: str, host_dir: str, host_state: str, uid: str, agent: dict, n: int, tag: str) -> int:
    """Append ``n`` log.added events to ``uid``; returns how many succeeded."""
    s = open_store(root, host_dir, host_state)
    ok = 0
    for i in range(n):
        s.append({"type": "log.added", "actor": agent, "text": f"{tag}-{i}"}, log=uid)
        ok += 1
    s.close()
    return ok


def create_tickets(root: str, host_dir: str, host_state: str, agent: dict, owner: str, n: int) -> list[str]:
    s = open_store(root, host_dir, host_state)
    keys = []
    for i in range(n):
        r = s.create_ticket(actor=agent, ticket_type="chore", title=f"t{i}", owner=owner)
        keys.append(r.event["key"])
    s.close()
    return keys


def crash_at(root: str, host_dir: str, host_state: str, uid: str, agent: dict, step: str) -> None:
    """Append one event and die (``os._exit``) at ``step`` of the write order. Used by the crash tests."""
    import os

    from orch.store import store as store_mod

    s = open_store(root, host_dir, host_state)

    def die(*a, **k):
        os._exit(7)

    if step == "after_pending":
        orig = store_mod.append_durable
        store_mod.append_durable = lambda *a, **k: die()  # type: ignore[assignment]
        _ = orig
    elif step == "after_append":
        s._finish = die  # type: ignore[method-assign]
    elif step == "mid_install":
        real = s._install

        def half(m, d, check=None):
            f0 = m["files"][0]
            dest = Path(root) / f0["to"]
            os.replace(d / str(f0["n"]), dest)
            die()
            return real(m, d)

        s._install = half  # type: ignore[method-assign]
    elif step == "torn_line":
        real_append = store_mod.append_durable

        def torn(path, data, **kw):
            real_append(path, data[: len(data) // 2])
            die()

        store_mod.append_durable = torn  # type: ignore[assignment]
    s.append({"type": "log.added", "actor": agent, "text": "dying"}, log=uid)
    time.sleep(0.1)
    sys.exit(0)


def crash_update(
    root: str, host_dir: str, host_state: str, uid: str, agent: dict, base_rev: dict, mode: str = "mid_install"
) -> None:
    """ticket.updated of title and a section; die after the first projection file was renamed into place."""
    import os

    from orch.store.render import section_entry

    s = open_store(root, host_dir, host_state)
    real = s._install

    def half(m, d, check=None):
        f0 = m["files"][0]
        dest = Path(root) / f0["to"]
        os.replace(d / str(f0["n"]), dest)
        os._exit(7)
        return real(m, d)

    def die(*a, **k):
        os._exit(7)

    s._install = half if mode == "mid_install" else die  # type: ignore[method-assign]
    s._finish = die if mode == "after_append" else s._finish  # type: ignore[method-assign]
    from orch import canon

    ev = {
        "type": "ticket.updated",
        "actor": agent,
        "base_rev": {"ticket.title": canon.value_hash("A ticket"), "body.context": canon.section_hash("")},
        "set": {"ticket.title": "crashed title"},
        "sections": {"context": section_entry("crashed body")},
    }
    s.append(ev, log=uid, body={"context": "crashed body"})
