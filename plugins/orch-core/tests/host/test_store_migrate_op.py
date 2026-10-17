"""``host.store_migrate``: the host stops serving, the engine moves the store, the registry records ``store`` and the
workspace is served again from ``.orch``; a replayed idem answers from its record; a second run is a no-op."""

from __future__ import annotations

import os
from pathlib import Path

from orch.host import control
from orch.host.control_client import new_idem
from tests.host.human_helpers import lab  # noqa: F401  (fixture)
from tests.host.test_lifecycle import Backend, ok, serve_lab


def test_a_registered_workspace_is_migrated_and_served_from_dot_orch(lab) -> None:  # noqa: F811
    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        assert "host.store_migrate" in control.CONTROL_OPS
        idem = new_idem()
        out = ok(c.call("host.store_migrate", {"workspace_id": wid}, idem=idem))["data"]
        assert out == {"workspace_id": wid, "migrated": True}
        root = Path(lab.root)
        assert (root / ".orch" / "config.json").is_file() and not (root / "config.json").exists()
        entry = h.host.registry.get(wid)
        assert entry is not None and entry.store == ".orch"
        svc = h.host.services[wid]
        assert svc.state != "unavailable", svc.cause
        assert svc.store is not None and not svc.store.layout.legacy
        # the same idem answers from its record, a new one finds nothing left to do
        again = c.call("host.store_migrate", {"workspace_id": wid}, idem=idem)
        assert again["ok"] and again["result"].get("duplicate") is True
        second = ok(c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem()))["data"]
        assert second["migrated"] is False
        # the registry file carries the key
        doc = (Path(lab.state) / "host" / "workspaces.json").read_text()
        assert '"store":".orch"' in doc
        assert oct(os.stat(root / ".orch").st_mode & 0o777) == "0o700"
    finally:
        h.__exit__(None, None, None)


def test_an_unknown_workspace_is_not_found_and_a_refusal_keeps_serving(lab) -> None:  # noqa: F811
    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        res = c.call("host.store_migrate", {"workspace_id": "f" * 32}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "not_found"
        (Path(lab.root) / ".orch-migrating").mkdir()  # a leftover: the engine refuses before moving anything
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "migrate.refused"
        assert (Path(lab.root) / "config.json").is_file()
        assert h.host.services[wid].state != "unavailable"  # served again after the refusal
    finally:
        h.__exit__(None, None, None)


def test_a_records_enabled_workspace_is_refused_untouched(lab) -> None:  # noqa: F811
    from orch.host import records

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        records._write_record(
            Path(lab.state),
            wid,
            os.getuid(),
            {"v": 1, "private_remote": None, "exports": [], "repo": {"state": "ok"}},
        )
        assert records._configured(records.read_record(Path(lab.state), wid, os.getuid())), "fixture must be configured"
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "migrate.refused"
        assert (Path(lab.root) / "config.json").is_file() and not (Path(lab.root) / ".orch").exists()
        assert h.host.services[wid].state != "unavailable"
    finally:
        h.__exit__(None, None, None)


def test_a_host_start_finishes_a_committed_journal_and_rolls_back_an_unfinished_one(lab) -> None:  # noqa: F811
    """Recovery follows the migrate journal, not only the control intent: a host restarted over a half-done migration
    brings it to a terminal state before it serves."""
    from orch.store import migrate as mg

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    root = Path(lab.root)
    try:
        ok(c.call("host.unregister", {"workspace_id": wid}, idem=new_idem()))
    finally:
        h.__exit__(None, None, None)

    # a crash after the commit point: journal committed, store in .orch, registry not yet updated
    def stop_at_committed(p: str) -> None:
        if p == "committed":
            raise KeyboardInterrupt(p)

    h2 = lab.host(backend=lambda: backend)
    h2.__enter__()
    try:
        c2 = h2.client()
        wid = ok(lab.register(c2))["data"]["workspace_id"]
    finally:
        h2.__exit__(None, None, None)
    try:
        mg.migrate(root, state_dir=Path(lab.state), workspace_id=wid, fault=stop_at_committed)
    except KeyboardInterrupt:
        pass
    assert mg.journal_step(Path(lab.state), wid) == "committed" and (root / ".orch").is_dir()
    h3 = lab.host(backend=lambda: backend)
    h3.__enter__()
    try:
        assert ("host.store_migrate", "done") in h3.host.recovered
        entry = h3.host.registry.get(wid)
        assert entry is not None and entry.store == ".orch"
        assert h3.host.services[wid].state != "unavailable"
    finally:
        h3.__exit__(None, None, None)
