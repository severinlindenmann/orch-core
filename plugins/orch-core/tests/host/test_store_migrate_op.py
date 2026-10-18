"""``host.store_migrate``: the host stops serving, the engine moves the store, the registry records ``store`` and the
workspace is served again from ``.orch``; a replayed idem answers from its record; a second run is a no-op."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


def test_records_enabled_with_an_unidentifiable_repository_is_refused_untouched(lab) -> None:  # noqa: F811
    from orch.host import records

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        (Path(lab.root) / ".git").mkdir()  # not a repository the reader accepts
        records._write_record(
            Path(lab.state),
            wid,
            os.getuid(),
            {"v": 1, "private_remote": None, "exports": [], "repo": {"state": "ok"}},
        )
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "migrate.refused"
        assert (Path(lab.root) / "config.json").is_file() and not (Path(lab.root) / ".orch").exists()
        assert h.host.services[wid].state != "unavailable"
    finally:
        h.__exit__(None, None, None)


def test_pending_sign_requests_are_counted_from_the_file_store_when_no_service_is_loaded(lab, monkeypatch) -> None:  # noqa: F811
    from orch.host.requests import RequestStore

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        monkeypatch.setattr(h.host, "services", {})  # no service object for the workspace
        monkeypatch.setattr(RequestStore, "open_count", lambda self: 2)
        with pytest.raises(Exception) as exc:
            h.host.control.store_migrate({"workspace_id": wid}, new_idem())
        assert "migrate.refused" in str(exc.value) or "2 sign request" in str(exc.value)
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


def test_running_work_is_refused_and_named(lab, monkeypatch) -> None:  # noqa: F811
    from orch.host.runners import RunnerHub

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        monkeypatch.setattr(RunnerHub, "running_work", lambda self, w: {"runs": 2, "terminals": 1})
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "migrate.refused"
        assert "2 agent or runner session" in res["error"]["message"] and "1 terminal" in res["error"]["message"]
        assert (Path(lab.root) / "config.json").is_file() and not (Path(lab.root) / ".orch").exists()
        monkeypatch.undo()
        service = h.host.services[wid]
        monkeypatch.setattr(type(service.requests), "open_count", lambda self: 3)
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["error"]["code"] == "migrate.refused" and "3 sign request" in res["error"]["message"]
        assert h.host.services[wid].state != "unavailable"
    finally:
        h.__exit__(None, None, None)


def test_the_cli_refuses_a_host_that_does_not_list_the_op(lab, monkeypatch) -> None:  # noqa: F811
    from orch import migrate_cli

    class Old:
        welcome = {"ops": {"host.list": 1}}

        def close(self) -> None:
            pass

    from orch.ops import workspace_init as wi

    monkeypatch.setattr(wi, "connect_host", lambda sd: Old())
    with pytest.raises(migrate_cli.MigrateCliError) as exc:
        migrate_cli._via_host({}, Path(lab.state), "a" * 32)  # noqa: SLF001
    assert exc.value.code == "migrate.version_skew" and "orch host install" in (exc.value.hint or "")


def test_a_start_arriving_after_the_first_look_is_caught_by_the_check_after_the_unserve(lab, monkeypatch) -> None:  # noqa: F811
    """The check runs unserved and under owner.lock, so a run that starts between a first look and the move cannot slip
    past: here it appears right after the unserve, and the migration refuses, serves again and moves nothing."""

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        real = h.host.unserve_from_thread

        def unserve_then_a_start(workspace_id: str) -> None:
            real(workspace_id)
            h.host.runners.starting["r_late"] = ("spec", None)  # type: ignore[assignment]

        monkeypatch.setattr(h.host, "unserve_from_thread", unserve_then_a_start)
        res = c.call("host.store_migrate", {"workspace_id": wid}, idem=new_idem())
        assert res["ok"] is False and res["error"]["code"] == "migrate.refused"
        assert "1 agent or runner session" in res["error"]["message"]
        assert (Path(lab.root) / "config.json").is_file() and not (Path(lab.root) / ".orch").exists()
        assert h.host.services[wid].state != "unavailable"  # served again after the refusal
    finally:
        h.host.runners.starting.clear()
        h.__exit__(None, None, None)


def test_a_registered_workspace_with_a_committed_journal_and_no_host_is_host_required_then_the_host_finishes(
    lab,  # noqa: F811
) -> None:

    from orch.store import migrate as mg

    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    root = Path(lab.root)
    h.__exit__(None, None, None)  # the host goes down with the workspace still registered

    def stop_at_committed(p: str) -> None:
        if p == "committed":
            raise KeyboardInterrupt(p)

    with pytest.raises(KeyboardInterrupt):
        mg.migrate(root, state_dir=Path(lab.state), workspace_id=wid, fault=stop_at_committed)
    assert mg.journal_step(Path(lab.state), wid) == "committed"
    from orch.ops.caps import Files
    from orch.ops.errors import OrchError
    from orch.ops.runtime import Workspace

    env = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(lab.state), "HOME": str(lab.tmp)}
    with pytest.raises(OrchError) as exc:
        Workspace(env, files=Files.direct()).store  # noqa: B018
    assert exc.value.code == "host.required"
    assert mg.journal_step(Path(lab.state), wid) == "committed"  # the direct command did not finish it
    h2 = lab.host(backend=lambda: backend)
    h2.__enter__()
    try:
        assert ("host.store_migrate", "done") in h2.host.recovered
        assert h2.host.registry.get(wid).store == ".orch"
        assert h2.host.services[wid].state != "unavailable"
    finally:
        h2.__exit__(None, None, None)
