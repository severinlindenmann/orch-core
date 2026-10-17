"""``orch migrate``: move this workspace's store from the project root into ``<project>/.orch/`` (design B11-P2 3).

    orch migrate [--json]

A person-side tool like ``orch worktree`` (not a registry operation, so no operation goldens). Two paths, one engine
(:mod:`orch.store.migrate`):

* **A host serves the workspace** (registered): the request goes to the host's control socket as ``host.store_migrate``
  (idem, outcome, running-work refusals, ``owner.lock`` and the store lock, restart recovery). A host that does not list
  that op is older than this CLI and does not know the ``store`` key: refused (``migrate.version_skew``) before
  anything is sent, with the fix.
* **Direct mode** (no host): the CLI takes ``owner.lock`` exclusively and runs the engine itself.

An unfinished migration (a crash) is completed or rolled back by the same command: the journal is found by the project's
identity even when the store is half way and discovery sees nothing. A call under ``ORCH_GRANT`` is refused:
agents never move a store. Records-enabled workspaces are refused until the records repo move lands
(``migrate.refused``).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

__all__ = ["main"]

LOCK_WAIT = 10.0
_USAGE = "usage: orch migrate [--json]"


class MigrateCliError(Exception):
    def __init__(self, code: str, message: str, exit_: int = 3, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.exit, self.hint = code, message, exit_, hint


def _find_interrupted(state_dir: Path, start: Path) -> tuple[str, Path] | None:
    """A non-terminal migrate journal whose recorded project identity is ``start`` or one of its ancestors: the
    workspace id and the project path. Used when the store is half way and discovery finds no config."""
    import stat as _stat

    from orch.store import migrate as mg

    hosts = state_dir / "hosts"
    try:
        names = sorted(os.listdir(hosts))
    except OSError:
        return None
    chain = [start, *start.parents]
    idents: dict[tuple[int, int], Path] = {}
    for p in chain:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if _stat.S_ISDIR(st.st_mode):
            idents[(st.st_dev, st.st_ino)] = p
    for wid in names:
        try:
            doc = mg._read_journal(hosts / wid / mg.JOURNAL)  # noqa: SLF001
        except mg.MigrateError:
            continue
        if doc is None or doc["step"] in ("done", "rolled_back"):
            continue
        root = doc.get("root")
        if isinstance(root, list) and len(root) == 2 and (root[0], root[1]) in idents:
            return wid, idents[(root[0], root[1])]
    return None


def _records_enabled(state_dir: Path, wid: str) -> bool:
    from orch.host import records
    from orch.host.errors import HostError

    try:
        return bool(records._configured(records.read_record(state_dir, wid, os.getuid())))  # noqa: SLF001
    except HostError:
        return True  # an unreadable records state is "enabled": fail closed


def _via_host(env: Mapping[str, str], sd: Path, wid: str) -> dict[str, Any]:
    from orch.host.control_client import new_idem
    from orch.ops import workspace_init as wi

    try:
        client = wi.connect_host(sd)
    except (OSError, ConnectionError, ValueError, TimeoutError) as exc:
        raise MigrateCliError(
            "host.unavailable",
            f"the workspace is served by the orch host, which cannot be reached ({type(exc).__name__})",
            hint="start the host (orch host run, or the service) and run `orch migrate` again",
        ) from None
    try:
        ops = (client.welcome or {}).get("ops") or {}
        if "host.store_migrate" not in ops:
            raise MigrateCliError(
                "migrate.version_skew",
                "the running host is older than this orch and does not know the .orch store layout",
                hint="update the host (orch host install), restart it, then run `orch migrate` again",
            )
        res = wi._call(client, "host.store_migrate", {"workspace_id": wid}, idem=new_idem())  # noqa: SLF001
    except wi._Lost as exc:  # noqa: SLF001
        raise MigrateCliError(
            "host.unavailable",
            f"the host did not answer ({exc}); the migration may be running",
            exit_=9,
            hint="run `orch migrate` again: it continues from the journal",
        ) from None
    finally:
        client.close()
    if res.get("ok"):
        data = ((res.get("result") or {}).get("data")) or {}
        return {"migrated": bool(data.get("migrated")), "via": "host"}
    err = res.get("error") or {}
    raise MigrateCliError(
        str(err.get("code", "internal")), str(err.get("message", "the host refused")), int(res.get("exit", 3))
    )


def _direct(sd: Path, project: Path, wid: str) -> dict[str, Any]:
    from orch.host.errors import HostError
    from orch.host.ownership import OwnershipLock
    from orch.store import migrate as mg
    from orch.store.pins import HostPins

    if _records_enabled(sd, wid):
        raise MigrateCliError("migrate.refused", "records are enabled for this workspace; nothing was moved")
    pins = HostPins(sd, wid)
    try:
        pin = pins.genesis()
    except Exception:  # noqa: BLE001
        raise MigrateCliError("trust.genesis_mismatch", "the genesis pin of this workspace cannot be read") from None
    finally:
        pins.close()
    try:
        with OwnershipLock(sd, wid).exclusive(LOCK_WAIT):
            out = mg.migrate(project, state_dir=sd, workspace_id=wid, expected_genesis=pin)
    except HostError as exc:
        raise MigrateCliError(exc.code, exc.message, 9) from None
    except mg.MigrateError as exc:
        raise MigrateCliError(exc.code, exc.detail, 9 if exc.code == "migrate.busy" else 3) from None
    return {"migrated": bool(out["migrated"]), "via": "direct"}


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    as_json = "--json" in argv

    def fail(exc: MigrateCliError) -> int:
        if as_json:
            doc = {"ok": False, "code": exc.code, "message": exc.message}
            if exc.hint:
                doc["hint"] = exc.hint
            out.write(json.dumps(doc) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n" + (f"hint: {exc.hint}\n" if exc.hint else ""))
        return exc.exit

    try:
        if os.name == "nt":
            raise MigrateCliError("unsupported.platform", "orch migrate needs POSIX filesystem semantics")
        if "ORCH_GRANT" in env:
            raise MigrateCliError("op.not_allowed", "an agent never moves a store; this tool is for the person")
        if [a for a in argv if a != "--json"]:
            raise MigrateCliError("invalid.input", _USAGE, 2)
        from orch import discovery
        from orch.host.ownership import is_registered
        from orch.layout import LayoutError
        from orch.ops.caps import Files
        from orch.ops.runtime import state_dir

        sd = state_dir(env, Files.direct())
        start = os.getcwd()
        explicit = env.get("ORCH_WORKSPACE")
        try:
            found = discovery.locate(explicit or start, explicit=bool(explicit), v1=False)
        except LayoutError as exc:
            raise MigrateCliError("migrate.refused", str(exc), hint=exc.hint) from None
        wid: str | None = None
        project: Path | None = None
        if found is not None:
            wid = ((found.config or {}).get("workspace") or {}).get("id")
            project = found.project
        interrupted = _find_interrupted(sd, Path(os.path.realpath(explicit or start)))
        if interrupted is not None and (wid is None or interrupted[0] == wid):
            wid, project = interrupted
        if not isinstance(wid, str) or project is None:
            raise MigrateCliError("not_found", "no orch workspace here", 2)
        already = found is not None and found.layout is not None and not found.layout.legacy
        if already and interrupted is None:
            return _done(out, as_json, {"migrated": False, "via": "none"})
        result = _via_host(env, sd, wid) if is_registered(sd, wid) else _direct(sd, project, wid)
    except MigrateCliError as exc:
        return fail(exc)
    return _done(out, as_json, result)


def _done(out: TextIO, as_json: bool, result: dict[str, Any]) -> int:
    if as_json:
        out.write(json.dumps({"ok": True, **result}) + "\n")
    elif result["migrated"]:
        out.write(
            "ok migrate: the store now lives in .orch/ (git-ignored); the logs are unchanged\nnext: orch status\n"
        )
    else:
        out.write("ok migrate: nothing to do, the store is already in .orch/\nnext: orch status\n")
    return 0
