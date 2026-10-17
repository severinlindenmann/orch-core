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
